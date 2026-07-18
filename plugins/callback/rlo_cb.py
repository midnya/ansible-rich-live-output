from collections.abc import Sequence
import curses
import datetime
import json
import os
import pathlib
import re
import string
import sys
import time

import rich
import yaml

from ansible_collections.midnya.rlo.plugins.module_utils.transformers import init_transformer, get_transform_callback

from ansible.executor.task_result import TaskResult
from ansible.inventory.host import Host
from ansible.module_utils.common.text.converters import to_text
from ansible.parsing.yaml.dumper import AnsibleDumper
from ansible.plugins.callback import CallbackBase, strip_internal_keys, module_response_deepcopy

from rich import box
from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.pretty import Pretty
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback
from rich.theme import Theme
from rich.console import Console


DOCUMENTATION = '''
    author: Midnya <github@midnya.cat>
    name: rlo_cb
    type: stdout
    short_description: Rich Live Output
    description:
      - Rich stdout callback plugin. Designed for humans, using modern terminals, with a focus on density of useful information.
    extends_documentation_fragment:
      - default_callback
    requirements:
      - set as rlo_cb in configuration
    options:
      rlo_display_fqdn:
        description: 
          - Whether to strip an action's FQDN.
          - V(full): Display the action name as received.
          - V(abbreviated): C(ansible.builtin.debug) -> C(a.b.debug).
          - V(stripped): C(ansible.builtin.debug) -> C(debug).
        choices: [full, abbreviated, stripped]
        default: stripped
        env:
          - name: RLO_DISPLAY_FQDN
        vars:
          - name: rlo_display_fqdn
        type: string
      rlo_transformer:
        description: A transformer function applied before printing values to the terminal.
            Formatted as "<module>/<class_name>". If "<module>" is empty, `rlo.transformers` is used.
        default: /Identity
        env:
          - name: RLO_TRANSFORMER
        vars:
          - name: rlo_transformer
        type: string
'''


# From community.general.yaml callback
def get_yaml_string_from_result(result):
    dumped = ''
    if result:
        dumped = to_text(yaml.dump(result, allow_unicode=True, width=1000, Dumper=MyDumper, default_flow_style=False))
    dumped = '  ' + '\n  '.join(dumped.split('\n')).rstrip()

    return dumped

## Midnya: I'm not going to pretend I understand why the below is needed.
## This has been taken from community.general.yaml some time after ansible-core 2.19 released.
## Adapated to fit RLO's use case.
try:
    from ansible.module_utils.common.yaml import HAS_LIBYAML
    # import below was added in https://github.com/ansible/ansible/pull/85039,
    # first contained in ansible-core 2.19.0b2:
    from ansible.utils.vars import transform_to_native_types

    if HAS_LIBYAML:
        from yaml.cyaml import CSafeDumper as SafeDumper
    else:
        from yaml import SafeDumper

    class MyDumper(SafeDumper):
        pass

except ImportError:
    # In case transform_to_native_types cannot be imported, we either have ansible-core 2.19.0b1
    # (or some random commit from the devel or stable-2.19 branch after merging the DT changes
    # and before transform_to_native_types was added), or we have a version without the DT changes.

    # Here we simply assume we have a version without the DT changes, and thus can continue as
    # with ansible-core 2.18 and before.

    transform_to_native_types = None

    from ansible.parsing.yaml.dumper import AnsibleDumper

    class MyDumper(AnsibleDumper):  # pylint: disable=inherit-non-class
        pass

def should_use_block(value):
    """Returns true if string should be in block format"""
    for c in u"\u000a\u000d\u001c\u001d\u001e\u0085\u2028\u2029":
        if c in value:
            return True
    return False


def _yaml_dumper_represent_scalar(self, tag, value, style=None):
    """Uses block style for multi-line strings"""
    if style is None:
        if should_use_block(value):
            style = '|'
            # we care more about readable than accuracy, so...
            # ...no trailing space
            value = value.rstrip()
            # ...and non-printable characters
            value = ''.join(x for x in value if x in string.printable or ord(x) >= 0xA0)
            # ...tabs prevent blocks from expanding
            value = value.expandtabs()
            # ...and odd bits of whitespace
            value = re.sub(r'[\x0b\x0c\r]', '', value)
            # ...as does trailing space
            value = re.sub(r' +\n', '\n', value)
        else:
            style = self.default_style
    node = yaml.representer.ScalarNode(tag, value, style=style)
    if self.alias_key is not None:
        self.represented_objects[self.alias_key] = node
    return node
# End of community.general.yaml


setattr(MyDumper, 'represent_scalar', _yaml_dumper_represent_scalar)

DEFAULT_CNORM = b"\x1b[?25h"
def get_cnorm():
    try:
        raise RuntimeError
        cnorm = curses.tigetstr("cnorm")
        # cnorm isn't defined, fallback to the default.
        if cnorm is None:
            return DEFAULT_CNORM
        return cnorm
    except:
        # Couldn't query terminfo for whatever reason, fallback to the default.
        # Most likely caused by setupterm(3) not being called, but that's not RLO's responsability.
        return DEFAULT_CNORM

def transform_dict(data, callback):
    if isinstance(data, str):
        return callback(data)
    elif isinstance(data, dict):
        return dict([(transform_dict(key, callback), transform_dict(data[key], callback)) for key in data])
    elif isinstance(data, tuple):
        return (transform_dict(d, callback) for d in data)
    elif isinstance(data, list):
        return [transform_dict(d, callback) for d in data]
    elif data is None or isinstance(data, (int, float, bool)):
        return data
    else:
        return f"<RLO error - unrecognized type - {type(data)}>"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'rlo_cb'

    SYMBOL_OK = "✔" # U+2714 - Heavy Check Mark
    SYMBOL_CHANGED = "⚙" # U+2699 - Gear 
    SYMBOL_SKIPPED = "⏭" # U+23ED - Black Right-Pointing Double Triangle with Vertical Bar
    SYMBOL_FAILED = "✘" # U+2718 - Heavy Ballot X
    SYMBOL_UNREACHABLE = "🖧" # U+1F5A7 - Three Networked Computers

    def __init__(self):
        super(CallbackModule, self).__init__()

        # Prepare for __del__
        self.__DEL_CNORM = get_cnorm().decode("ascii")
        self.__DEL_STDOUT = sys.__stdout__

        # We do not have access to the options values at __init__,
        # so we do the next best possible thing in _transform
        self._transformer_user = None
        self._transformer_user_callback = None

        self._transformer_sanitizer = init_transformer("ansible_collections.midnya.rlo.plugins.module_utils.transformers", "Sanitizer")
        self._transformer_sanitizer_callback = get_transform_callback(self._transformer_sanitizer)

        self._hosts = {}
        self._current_role = None
        self._last_printed_role = None
        self._should_print_role = False
        self._start_time = datetime.datetime.now()

        self._interactive = bool(int(os.environ.get('RLO_FORCE_INTERACTIVE', "1")))
        enable_timer = bool(int(os.environ.get('RLO_ENABLE_TIMER', "1")))

        theme = Theme({
            "rlo.task.log.changed": "yellow",
            "rlo.task.log.failed": "red",
            "rlo.task.log.unreachable": "red",
            "rlo.task.log.retried": "bright_black",
            "rlo.task.log.skipped": "blue",

            "rlo.task.log.task_path": "bright_black",

            "rlo.task.result.ok": "green",
            "rlo.task.result.changed": "yellow",
            "rlo.task.result.failed": "red",
            "rlo.task.result.unreachable": "red",
            "rlo.task.result.skipped": "blue",

            "rlo.time": "green",
            "progress.elapsed": "green italic",

            "table.header": "bold",
            "table.footer": "bold",
            "table.cell": "none",
            "table.title": "bold",

            "repr.ipv4": "magenta",
            "repr.ipv6": "magenta",
            "repr.path": "magenta",
            "repr.filename": "magenta",
        }, inherit=False)
        console = Console(theme=theme)

        self._progress = Progress(
            TextColumn("[bold]{task.fields[host]}[/bold]"),
            TextColumn("-"),
            TextColumn("{task.description}"),
            TextColumn("-"),
            TimeElapsedColumn(),
            transient=True,
            console=console
        )

        self._role_progress = Progress(
            TextColumn("[bold][italic]{task.description}"),
            TextColumn("-"),
            TimeElapsedColumn(),
            transient=True,
            console=console
        )
        self._role_progress_task_id = self._role_progress.add_task("None")

        self._live = Live(
            Group(
                Panel(
                    Group(
                        self._role_progress,
                        self._progress
                    ),
                box=box.MINIMAL, height=8)
            ),
         transient=True,
         auto_refresh=enable_timer,
         console=console)

        if self._interactive:
            self._live.start()

    def __del__(self):
        try:
            if self._interactive:
                self.__DEL_STDOUT.write(self.__DEL_CNORM)
                self.__DEL_STDOUT.flush()
        except:
            pass

    def v2_playbook_on_play_start(self, play):
        message = f"[bold] - Playbook - {play.get_name()} -[/bold]"
        if play.check_mode:
            message = f"{message} [italic]Check Mode[/italic]"
        self._log(message)

    def v2_runner_on_start(self, host, task, **kwargs):
        (host_name, host_label, task_desc, role_name) = self._get_task_infos(host, task)
        self._handle_new_task(host_name, host_label, task_desc, role_name)
        self._live.refresh()

    def v2_runner_on_ok(self, result, **kwargs):
        self._on_finished_task(result, 'ok', **kwargs)

    def v2_runner_on_skipped(self, result, **kwargs):
        self._on_finished_task(result, 'skipped', **kwargs)

    def v2_runner_on_failed(self, result, **kwargs):
        self._on_finished_task(result, 'failed', **kwargs)

    def v2_runner_on_unreachable(self, result, **kwargs):
        self._on_finished_task(result, 'unreachable', **kwargs)


    def v2_playbook_on_stats(self, stats):
        self._live.stop()

        delta = datetime.datetime.now() - self._start_time
        seconds = delta.total_seconds()

        table = Table(title=f"Play Recap - [rlo.time]{CallbackModule._format_float_seconds(seconds)}", box=box.MINIMAL, title_justify="right")

        table.add_column("[bold]host", justify="right")
        table.add_column("[bold]ok", style="rlo.task.result.ok")
        table.add_column("[bold]changed", style="rlo.task.result.changed")
        table.add_column("[bold]unreachable", style="rlo.task.result.failed")
        table.add_column("[bold]failed", style="rlo.task.result.unreachable")
        table.add_column("[bold]skipped", style="rlo.task.result.skipped")
        table.add_column("[bold]rescued")
        table.add_column("[bold]ignored")

        hosts = sorted(stats.processed.keys())
        for host in hosts:
            host_stats = stats.summarize(host)

            if host_stats['failures'] != 0 or host_stats['unreachable'] != 0:
                host_color = "rlo.task.result.failed"
            elif host_stats['changed'] != 0:
                host_color = "rlo.task.result.changed"
            else:
                host_color = "rlo.task.result.ok"

            table.add_row(
                f"[bold][{host_color}]{host}",
                str(host_stats['ok']),
                str(host_stats['changed']),
                str(host_stats['unreachable']),
                str(host_stats['failures']),
                str(host_stats['skipped']),
                str(host_stats['rescued']),
                str(host_stats['ignored']),
            )
        self._print()
        self._print(table)

    def v2_playbook_on_no_hosts_matched(self):
        self._log_task("[rlo.task.log.unreachable][bold]No hosts matched")

    def v2_runner_retry(self, result):
        (host_name, host_label, task_desc, role_name) = self._get_task_infos(result._host, result._task)
        message = f"[rlo.task.log.retried][bold]{host_label}[/bold] - {task_desc} - [bold]Failed - Retrying... ({result._result['retries'] - result._result['attempts']} retries left)[/bold]"
        self._log_task(message)

    def v2_playbook_on_notify(self, handler, host):
        (host_name, host_label, task_desc, role_name) = self._get_task_infos(host, handler)
        message = f"[italic][bold]notified[/bold] - {task_desc} [/italic]"
        self._log(message)

    # We are either asked to display skipped hosts, or the run is verbose enough (-vvv)
    def _should_log_skipped_task(self, result):
        return self.get_option('display_skipped_hosts') or self._run_is_verbose(result._result, 2)

    def _should_log_task(self, result, status):
        # The run is verbose enough (-vv)
        if self._run_is_verbose(result._result, 1):
            return True

        # The task is skipped and context allows it to be logged
        if status == 'skipped' and self._should_log_skipped_task(result):
            return True

        # The task is a 'debug' action that doesn't define "no_log" to True
        if (result._task.action == 'debug' or result._task.action == 'ansible.builtin.debug') and not result._task.no_log:
            # The debug task is skipped and context allows it to be logged
            if status == 'skipped':
                return self._should_log_skipped_task(result)

            return True

        # The task result is a failure
        if status == 'failed' or status == 'unreachable':
            return True

        # The task is ok, and either the tasked is changed, we are asked to display skipped hosts, or the run is verbose enough (-vvv)
        if status == 'ok' and (result.is_changed() or self.get_option('display_ok_hosts') or self._run_is_verbose(result._result, 2)):
            return True

        return False

    def _should_print_comprehensive_task_result(self, result, status):
        # The run is verbose enough (-vvvv)
        if self._run_is_verbose(result._result, 3):
            return True

        # The task result is failed, but explicitely not an unreachable
        if status == 'failed':
            return True

        return False

    def _should_print_reduced_task_result(self, result, status):
        # The run is verbose enough (-vv)
        if self._run_is_verbose(result._result, 1):
            return True
        # The task succeeded and changed something
        if status == 'ok' and result.is_changed():
            return True

        # The task is a 'debug' action that doesn't define "no_log" to True
        if (result._task.action == 'debug' or result._task.action == 'ansible.builtin.debug') and not result._task.no_log:
            return True

        return False

    def _on_finished_task(self, result, status, **kwargs):
        (host_name, host_label, task_desc, role_name) = self._get_task_infos(result._host, result._task)
        if self._hosts[host_name] in self._progress._tasks:
            elapsed = self._progress._tasks[self._hosts[host_name]].elapsed
            self._progress.remove_task(self._hosts[host_name])
        else:
            elapsed = 0

        self._handle_role(role_name)

        if self._should_log_task(result, status):
            message = f"[bold]{host_label}[/bold] - {task_desc}"
            if status == 'ok':
                if result.is_changed():
                    message = f"[rlo.task.log.changed]{self.SYMBOL_CHANGED} {message} - [bold]changed[/bold]"
                else:
                    message = f"{self.SYMBOL_OK} {message}"
            elif status == 'skipped':
                message = f"[rlo.task.log.skipped]{self.SYMBOL_SKIPPED} {message} - [bold]skipped[/bold]"
            elif status == 'unreachable':
                message = f"[rlo.task.log.unreachable]{self.SYMBOL_UNREACHABLE} {message} - [bold]unreachable[/bold]"
            elif status == 'failed':
                message = f"[rlo.task.log.failed]{self.SYMBOL_FAILED} {message} - [bold]failed[/bold]"

            self._log_task(message, elapsed)

        if result.is_changed():
            self._print_diff(result)

        # Only one kind of result should be printed depending on the task and context
        if self._should_print_comprehensive_task_result(result, status):
            self._print_result(result._result, result._task, self._get_comprehensive_result_object)
        elif self._should_print_reduced_task_result(result, status):
            self._print_result(result._result, result._task, self._get_reduced_result_object)

        if status == 'failed' and self.get_option('show_task_path_on_failure'):
            self._print(f"[rlo.task.log.task_path][italic]task path: {result._task.get_path()}")

    def _get_role_progress(self, role_name):
        if role_name == "None":
            return "<none>"
        return role_name

    def _handle_new_task(self, host_name, host_label, task_desc, role_name):
        self._hosts[host_name] = self._progress.add_task(task_desc, total=1, host=host_label)
        self._role_progress.update(self._role_progress_task_id, description=self._get_role_progress(role_name))

    def _handle_role(self, role_name):
        if self._current_role != role_name:
            self._should_print_role = True
            self._current_role = role_name

        if role_name == "None":
            self._should_print_role = False

        if self._last_printed_role == self._current_role:
            self._should_print_role = False

    def _print(self, *args, raw=False):
        if raw:
            self._progress.console.out(highlight=False, *args)
        else:
            self._progress.console.print(soft_wrap=True, *args)
        self._live.refresh()

    def _log(self, *args):
        self._print(CallbackModule._get_current_time_formatted(), *args)

    def _log_task(self, message, elapsed = None):
        if self._should_print_role:
            self._log(f"[bold] --- Role - {self._current_role} ---[/bold]")
            self._last_printed_role = self._current_role
            self._should_print_role = False

        if elapsed is None:
            self._log(message)
        else:
            self._log(message, "-", Text(CallbackModule._format_float_seconds(elapsed), style="rlo.time"))

    def _censored_no_log_response(self):
        return dict(censored="the output has been hidden due to the fact that 'no_log: true' was specified for this result (via RLO)")

    # Most is from community.general.yaml callback, `_dump_results`.
    # Returns all the necessary information for debugging a failed task.
    # Unless in very high verbosity, Ansible-internal variables are stripped.
    # Unless in high verbosity, information already printed via RLO are stripped (diff object, skipped/changed boolean)
    # Always strips redundant information (stdout/stderr lines if stdout/sterr already present).
    def _get_comprehensive_result_object(self, result, task):
        # Don't log a no_log task, unless we are verbose enough (-vvv)
        if task.no_log and not self._run_is_verbose(result, 2):
            return self._censored_no_log_response()

        # All result keys stating with _ansible_ are internal, so remove them from the result before we output anything.
        comprehensive_result = strip_internal_keys(module_response_deepcopy(result))

        if not self._run_is_verbose(result, 3) and 'invocation' in result:
            del comprehensive_result['invocation']
        if not self._run_is_verbose(result, 2):
            if 'diff' in result:
                del comprehensive_result['diff']
            if 'skipped' in comprehensive_result:
                del comprehensive_result['skipped']

        # if we already have stdout, we don't need stdout_lines
        if 'stdout' in comprehensive_result and 'stdout_lines' in comprehensive_result:
            comprehensive_result['stdout_lines'] = '<omitted>'
        # if we already have stderr, we don't need stderr_lines
        if 'stderr' in comprehensive_result and 'stderr_lines' in comprehensive_result:
            comprehensive_result['stderr_lines'] = '<omitted>'

        # Add task vars (those defined specifically at the task level) if there are some
        if task.vars:
            comprehensive_result["task_vars"] = task.vars

        return comprehensive_result

    def _get_inner_reduced_result_object(self, result):
        reduced_result = {}

        for key in ["stdout", "stderr"]:
            if key in result and result[key] != '':
                reduced_result[key] = result[key]

        # Probably an interesting thing to print
        if "msg" in result and result["msg"]:
            # These aren't, though
            if result["msg"] != "All items completed" and result["msg"] != "All items skipped":
                reduced_result["msg"] = result["msg"]

        return reduced_result

    # Returns a small object containing some information to debug a task.
    # Expected to be printed for each and every task, regardless of its status (ok, changed, failed, etc) on a medium verbosity.
    # Only prints msg, stdout, stderr, and task vars.
    # If the values are empty strings, they are omitted from the output.
    def _get_reduced_result_object(self, result, task):
        # Don't log a no_log task, unless we are verbose enough (-vvv)
        if task.no_log and not self._run_is_verbose(result, 2):
            return self._censored_no_log_response()

        reduced_result = {}

        if "results" in result:
            accumulator = []

            for inner_result in result["results"]:
                # Only add an entry to the printable list if it contains anything
                maybe_reduced_result = self._get_inner_reduced_result_object(inner_result)
                if maybe_reduced_result == {}:
                    continue
                accumulator.append(maybe_reduced_result)

            # Only print the results if there's anything to print
            if accumulator:
                reduced_result["results"] = accumulator


        reduced_result = reduced_result | self._get_inner_reduced_result_object(result)

        # Add task vars (those defined specifically at the task level) if there are some.
        if task.vars:
            reduced_result["task_vars"] = task.vars

        return reduced_result

    def _print_result(self, result, task, preprocessor):
        preprocessed_result = preprocessor(result, task)
        if preprocessed_result:
            sanitized_result = self._sanitize_dict(preprocessed_result)
            transformed_result = self._transform_dict(sanitized_result)
            yaml_result = get_yaml_string_from_result(transformed_result)
            escaped = escape(yaml_result)
            self._print(f"[bold][bright_magenta]{escaped}[/bold][/bright_magenta]")

    def _print_single_diff(self, result_object):
        diff_raw = result_object['diff']
        if not isinstance(diff_raw, list):
            diff_raw = [diff_raw]

        sanitized_difflist = [self._sanitize_dict(d) for d in diff_raw]
        transformed_difflist = [self._transform_dict(d) for d in sanitized_difflist]
        pretty_diff = self._get_diff(transformed_difflist) # This is the Ansible-provided function to pretty a diff
        pretty_diff = pretty_diff.strip(" \n")

        if pretty_diff:
            self._print(pretty_diff, raw=True)

    def _print_diff(self, result):
        if result._task.loop and 'results' in result._result:
            for res in result._result['results']:
                if 'diff' in res and res['diff'] and res.get('changed', False):
                    self._print_single_diff(res)

        elif 'diff' in result._result and result._result['diff'] and result._result.get('changed', False):
            self._print_single_diff(result._result)

    def _format_task_name(self, task):
        fqdn_display = self.get_option("rlo_display_fqdn")
        action = task.action

        if fqdn_display == "full":
            # No transform is needed
            pass
        elif fqdn_display == "stripped":
            # Keep the last part of the FQDN
            action = action.split(".")[-1]
        elif fqdn_display == "abbreviated":
            # Abbreivate the FQDN, *a la* Java's loggers package name shortening
            split_action = action.split(".")
            last_part = split_action.pop()

            action = ""
            for part in split_action:
                if len(part):
                    action += part[0] + "."
            action += last_part
        else:
            raise ValueError("`rlo_display_fqdn` option is not recognized!")


        if task.name:
            task_desc = f"[italic]{action}[/italic] - {task.name}"
        else:
            task_desc = f"[italic]{action}[/italic]"

        return task_desc

    def _get_task_infos(self, host, task):
        host_name = host.get_name()
        host_label = CallbackModule._get_host_label(host, task)
        task_desc = self._format_task_name(task)

        if CallbackModule._is_task_handler(task):
            task_desc = f"{task_desc} - [bold]handler[/bold]"

        if task.check_mode and self.get_option('check_mode_markers'):
            task_desc = f"{task_desc} - [italic]check[/italic]"

        if task._role:
            role_name = task._role._role_name
        else:
            role_name = "None"

        return (host_name, host_label, task_desc, role_name)

    def _run_is_verbose(self, result, verbosity=0):
        return self._display.verbosity > verbosity

    def _transform_dict(self, input):
        if self._transformer_user_callback is None:
            potential_transformer = self.get_option("rlo_transformer")
            parts = potential_transformer.split("/")

            if len(parts) != 2:
                raise ValueError("`rlo_transformer` option is malformed!")

            potential_transformer_module = parts[0]
            potential_transformer_name = parts[1]

            if potential_transformer_module == "":
                potential_transformer_module = "ansible_collections.midnya.rlo.plugins.module_utils.transformers"
            else:
                # Allow users to import their own Transformer relative to the execution directory.
                # I'm not aware of any better alternative?
                sys.path.append(str(pathlib.Path("./").absolute()))

            self._transformer_user = init_transformer(potential_transformer_module, potential_transformer_name)
            self._transformer_user_callback = get_transform_callback(self._transformer_user)
        return transform_dict(input, self._transformer_user_callback)

    def _sanitize_dict(self, input):
        return transform_dict(input, self._transformer_sanitizer_callback)

    @staticmethod
    def _is_task_handler(task):
        return str(task).startswith('HANDLER:')

    @staticmethod
    def _get_current_time_formatted():
        return "[bold]" + "[" + "[rlo.time]" + time.strftime('%H:%M:%S', time.localtime()) + "[/rlo.time]" + "]" + "[/bold]"

    @staticmethod
    def _get_host_label(host, task):
        label = "%s" % host.get_name()
        if task.delegate_to and task.delegate_to != host.get_name():
            label += " -> %s" % task.delegate_to
        return label

    @staticmethod
    def _format_float_seconds(n):
        milliseconds = int((n % 1) * 1000)

        if n < 1:
            return f".{milliseconds:0>3}"
        else:
            seconds = int(n % 60)
            minutes = int(n / 60) % 60
            hours = int(n / 3600) % 3600

            return f"{hours}:{minutes:0>2}:{seconds:0>2}.{milliseconds:0>3}"

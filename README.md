# Rich Live Output
> *RLO*, for short

An opinionated, information-dense stdout callback for Ansible.
Designed for humans with modern terminals.

## Main features

- **Rich and dense**: Terminal real estate comes at a premium; each line of log is meaningful.
- **Live output**: Know which tasks are currently running, and for how long.
- **Safe**: Sanitize the output before it arrives to your terminal.
- **Custom transformers**: Convert the output of your tasks before they're printed; ideal for censoring secrets, for example.

## Dependencies

- `rich`: 14 or later.
- `PyYAML`: 5.1 or later.
    - implicitely available if `ansible-core` is installed.
- `ansible-core`: initially developed for 2.13, extensive testing has not been done.

## Configuration

TODO: How to use  
TODO: How to enable/disable running tasks' timer (and why you may want to disable it)  
TODO: How to configure theme  
TODO: Complete reference of variables  

## TODOs (in no particular order):

- Finish this README
- Document rlo_cb more extensively
- Custom number of Live tasks
- Better output sanitization
- Custom themes
- Custom icons
- Scope as much hardcoded theming under rlo's theme namespace
- Multiple transformers, with priority ordering
- Entire run timer
- Detach the "what and when to log" from the "how to log" logic for integration in other tools
- Fix tasks vars
- Aynsc tasks support?
- Jinja template for task names??

## AI usage disclosure

- No LLM-generated code has been commited to this codebase.
  - LLM may have been used in 2024 and 2025 for debugging RLO;
    I honestly can't remember whether I used one or not.
- Some code snippets I did not author have been used.
  Given the timestamps attached to those snippets, I highly doubt they were LLM-generated;
  I cannot garantee it, however.
    - If there ever is doubt, all code snippets I do not author are sourced.
- If you wish to contribute to this repository, you are welcome to do so;
  I will not tolerate LLM-generated code, however.

## Authors

Rich Live Output was created by [ShinySaana](https://github.com/ShinySaana).  
Some code snippets were sourced from [community.general](https://github.com/ansible-collections/community.general)'s [`yaml` callback plugin](https://github.com/ansible-collections/community.general/blob/main/plugins/callback/yaml.py).

## License

GNU General Public License v3.0 or later

See [COPYING](COPYING) to see the full text.

# pyto-harness v1.0.8

Released 6 October 2026.

## Pyto-aware commands and libraries

The agent can ask `unix_capabilities` which allowlisted commands this Pyto build reports,
then use `unix_command` with a command name and separate arguments. It supports read-only
uses of `cat`, `cut`, `find`, `grep`, `head`, `ls`, `sort`, `tail`, `uniq` and `wc`; files
must be in the workspace. Inputs and directory scans are bounded. It does not accept shell
chains, pipes or redirection. Pyto documents that its `ios_system` commands run inside the
app process through `subprocess.Popen`; a command has no reliable hard kill timeout.

The new `python_module_capabilities` tool checks whether optional import names can be found
without importing them. `pyto_api` remains the source of grounded Pyto module members and
signatures. The agent is told to check command and module availability before building a
reusable tool or program that depends on them.

References: [Pyto terminal documentation](https://github.com/ColdGrub1384/Pyto/blob/main/docs/terminal.rst),
[Pyto Shortcuts and Run Command documentation](https://github.com/ColdGrub1384/Pyto/blob/main/docs/automation.rst).

## Persistent custom tools

`custom_tool_create` saves a schema-described Python function, `def run(inputs):`, under the
workspace's `custom-tools/` directory. The tool is added to the model's available tools on
the next turn and persists between launches. Source integrity is checked before every run;
the generic workspace file tools cannot edit the managed store. Creation and every later
invocation need approval, and the approval view includes the Python source. A saved tool
runs with Pyto's app permissions and is not a sandbox. `custom_tool_list`,
`custom_tool_disable` and `custom_tool_enable` manage the saved set.

## Verification

- `python3 -m unittest discover -s tests -t .`: 745 tests passed in 41.060 seconds. The
  suite uses an HTTP mock bound to `127.0.0.1`; it makes no production API requests.
- `python3 stdlib_audit.py --json`: passed; no third-party runtime imports, all 22 runtime
  files parse as Python 3.10.
- `python3 run.py --version`: reported `pyto-harness 1.0.8`.
- Targeted tests exercise command argument/path limits, custom tool persistence and
  approval, and dynamic tool availability in the following model turn.

Pyto/iOS device checks have not been run. The user will check v1.0.8 on-device after the
release is available. In particular, verify that this installed Pyto build's `help` output
is discovered, try a harmless `grep` or `wc` command in the workspace, check a known bundled
library with `python_module_capabilities`, and create/run/disable one small custom tool.

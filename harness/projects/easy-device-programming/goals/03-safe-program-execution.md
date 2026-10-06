# Goal 03: Keep program runs from interfering with each other

## Executable prompt

Work in this repository and read `AUDIT-2026-10-05.md` plus applicable repository instructions first. Inspect the current implementation; earlier goal work may have changed the cited code. Implement the outcome end to end rather than returning only a plan. Preserve existing user files and policy guarantees. Use supported Pyto documentation/source when native API behavior matters. Add focused regression tests for the behavior being changed and run the relevant existing tests (`python3 -m unittest discover -s tests -t .` for the full suite; it uses a localhost mock server). Do not claim native verification without a device. If device access is unavailable, complete desktop-verifiable work and leave an exact device checklist and the verification limitation. Finish with changed files, tests/results and remaining limitations.

**Outcome:** One generated program must not corrupt another run or make the chat silently unusable.

**Prerequisites:** None; required before interactive preview. Verify prerequisite behavior exists before depending on it; if absent, identify the unmet prerequisite instead of inventing its API.

**Starting points:** harness/tools_ios.py: _run_in_process, run_program; harness/loop.py: _dispatch; harness/tools.py.

**Implement:**

Implement one managed in-process execution lane with explicit ownership of stdout/stderr, argv, cwd and environment restoration. Track running, completed and still-running-after-deadline states. A surviving worker must retain ownership and prevent another conflicting launch until it exits or runtime restart is required. Treat cooperative cancellation honestly, including native blocking and child-thread limitations. Serialize conflicting file mutations, program launch dependencies and native modal operations while retaining safe independent read concurrency. Show understandable status and recovery steps. Do not claim a sandbox or guaranteed thread termination.

**Acceptance criteria:**

Use deterministic concurrency tests for simultaneous runs, write-then-run batches, exceptions and cancellation. Confirm correct output separation and global-state restoration. Simulate a native-blocked survivor and prove no second conflicting run launches; allow recovery after the survivor exits. Avoid leaving test threads or changed globals behind.

## Implementation record — local v1.0.9 candidate (unpublished)

The in-process runner now reserves one execution lease before changing process-wide state. A surviving run keeps that lease until it exits and cleanup restores stdout, stderr, argv, cwd and the environment. A competing run receives a clear refusal; workspace reads are also refused while an overdue worker could still change files. Program-created threads are detected and joined before process state is restored. File/resource scheduling preserves parallel reads while ordering conflicting writes, program launches and native operations in both registry batches and the agent loop.

Timeout reporting distinguishes a cooperative stop from a worker that remains active in native code or a child thread. The message explains that the program may still change files, conflicting operations stay blocked, and restarting Pyto is the recovery if it never exits. This is concurrency control only: generated code still has Pyto's authority, and Python cannot guarantee termination of native calls or threads.

Desktop verification: the full suite passed **768 tests**. Focused coverage includes concurrent run requests and output separation, write-then-run in one model batch, overlapping reads, exception/global-state cleanup, child-thread cleanup, cooperative timeout and an overdue native-blocked worker that refuses another launch and later releases the lane. The same-batch regression uses the localhost mock provider.

### Pyto device checklist — pending

Record device model, iOS version and Pyto version, then verify:

1. Run a short program with arguments; confirm output and `sys.argv` are correct, then run another program and confirm its output is separate.
2. Run a program that changes cwd and `os.environ`, then raises an exception. Confirm the original cwd, environment, argv, stdout and stderr are restored.
3. Run `import time; time.sleep(4); print('finished')` with a 0.5-second limit. While the first run is still active, start another program and confirm it is refused with the recovery explanation. After the sleeper exits, confirm a new run succeeds.
4. Start a program-created background thread that writes a workspace file. Confirm the harness waits for it or reports it still active, blocks conflicting workspace operations while it can write, and recovers after it exits.
5. While a program is active, request a native modal action and a workspace write. Confirm both wait/refuse safely and can be retried after the run ends.

Do not mark device verification complete until these checks have been run on the installed Pyto version.

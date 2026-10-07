# Browser interface

Run `python run.py --web` in Pyto to start a local browser front end for the harness. Pyto
opens the generated URL in Safari when it can. If automatic opening fails, the URL is
printed in the Pyto console; open it on the same device while the run is active. An optional
task passed on the command line is placed in the message box and is not sent until you press
**Send**.

## Continue or choose a session

Without `--resume`, the web page starts with a session chooser. **Continue most recent**
resumes the newest valid local session. The list below it lets you choose another saved
session, with its last message preview, date, model, and workspace. **Start a new session**
creates a separate log and leaves the previous sessions unchanged.

When a session is resumed, its saved conversation is restored as model context and the
recent user and assistant messages appear in Chat. The History view remains available for
the full projected conversation. To open a specific session directly and skip the chooser,
pass its log path to `--resume`; passing a directory resumes its most recently modified
`.jsonl` session.

The page has three views:

- **Chat** sends messages through the normal harness loop. **Stop turn** requests
  cancellation of the current model/tool operation.
- **Saved programs** lists registered workspace programs and builds forms from their
  validated input schemas. Runs use the harness's existing managed `run_program` or
  `preview_program` handler.
- **History** shows a bounded page of the durable session log.

Approvals appear in Chat with **Allow** and **Deny** controls. The `--yolo` option keeps
its existing behavior and bypasses approvals. Use **Stop web session** to cancel pending
approvals, stop the web server, and end the foreground run. Stopping the Pyto script also
closes the server through normal cleanup.

## Formatted assistant replies

Assistant prose in Chat renders a small Markdown subset: headings, paragraphs, strong and
emphasized text, inline code, fenced code blocks, links, blockquotes, and ordered or
unordered lists. Fenced code keeps its whitespace and is horizontally scrollable on narrow
screens. Nested lists and ordered-list starting numbers are retained. Tool progress, errors,
approvals, saved-program output, and session history remain plain text.

Raw HTML and Markdown images are not interpreted. The browser builds elements from a
bounded syntax tree using DOM APIs; all text is inserted as text nodes. Links are active
only for `http`, `https`, and `mailto` destinations. Other or malformed destinations show
their label without a link. No Markdown library or other resource is loaded from a CDN.
Malformed Markdown remains readable, and a very large formatted event falls back to plain
text to keep the event stream bounded.

## Local server and browser boundary

The HTTP server binds only to `127.0.0.1` and chooses an ephemeral port. The URL includes
a cryptographically random, per-run token. API requests also need that token in a header;
the server checks `Host` and same-origin `Origin`, limits JSON request bodies to 64 KiB,
serves only three fixed assets, disables caching, and applies a restrictive content
security policy. Request logging is disabled so tokenized paths are not printed. No
external scripts, fonts, images, or services are loaded. The provider API key and client
remain in the Python process.

The server exists only for this explicitly started session; it is not an always-on
service.

This is a same-device interface, not a remotely accessible service. Do not share its
tokenized URL. Closing Safari alone leaves the foreground Pyto run active; return to the
page and choose **Stop web session**, or stop the script in Pyto.

The UI buffers a bounded, scrubbed event stream in memory. It does not persist browser
messages separately; the existing session log remains the durable history. Program input
values are passed only to the selected run and are not added to the saved-program index by
this interface.

## Pyto background behavior

When the web front end is explicitly started, it creates a Pyto `BackgroundTask` so the
server can continue while Safari is foregrounded. It stops that task during cleanup.
Pyto's documented mechanism uses blank audio to keep a script alive in the background, so
iOS may show normal background-audio behavior. Pyto may still suspend or terminate work
under system memory or lifecycle pressure; the harness cannot promise a durable server
after the app is killed. A running native Python program also cannot be forcibly stopped
at an arbitrary instruction: Stop sets the harness cancellation event and cancels the
provider request, while already-running tool code must return cooperatively. On session
shutdown, the harness waits for its worker before closing session resources.

Saved file and folder fields accept paths readable by Pyto. Safari file selection does
not grant the Python process a document-picker permission, so these web forms deliberately
ask for an accessible path instead of pretending an upload selection is available to
Pyto.

Pyto's reference implementation and API describe both the local web-server pattern and
the background keepalive mechanism: [web server sample](https://github.com/ColdGrub1384/Pyto/blob/main/Pyto/Samples/Examples/web_server.py),
[BackgroundTask API](https://github.com/ColdGrub1384/Pyto/blob/main/Lib/background.py).

## Device checklist

These checks require a real Pyto/iOS device and have not been run from the desktop test
environment:

1. Launch `run.py --web` and confirm Safari opens the tokenized `127.0.0.1` URL.
2. Without `--resume`, continue a previous session and confirm recent messages appear in
   Chat and the full conversation appears in History; start a fresh session and confirm its
   history starts empty.
3. Repeat with an explicit `--resume` path and confirm the session chooser is skipped.
4. Send a chat message and confirm the response appears without freezing Pyto.
5. Trigger an approval-required action; verify Allow executes it and Deny blocks it.
6. Start a slow request and confirm **Stop turn** returns the page to an idle state.
7. Open Saved programs and run one without an input schema.
8. Run a saved program with text, number, and choice fields; check required and optional
   values.
9. Enter an accessible file or folder path and verify it reaches the saved program.
10. Open History and confirm messages from this session appear.
11. Background Pyto while Safari is active, return to Pyto, and confirm the server is still
   active and cleanup works after **Stop web session**.
12. Close Safari without pressing Stop, then return and stop the session; separately stop
    the script from Pyto and confirm the port is released.

Desktop tests exercise the HTTP routes, authentication checks, chat loop, approvals,
saved-program execution, and shutdown using a loopback mock provider. They do not verify
Safari rendering, Pyto's background audio behavior, iOS suspension, or real file-picker
permissions.

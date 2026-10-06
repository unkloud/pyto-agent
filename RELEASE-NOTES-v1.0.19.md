# pyto-agent v1.0.19

## Formatted assistant replies in the browser interface

Assistant replies in the optional `--web` interface now render a bounded Markdown
subset, including headings, paragraphs, emphasis, inline and fenced code, blockquotes,
links, and ordered or unordered lists. Code formatting preserves whitespace and scrolls
horizontally on narrow screens. Tool output, approvals, saved-program output, errors, and
session history remain plain text.

Raw HTML and Markdown images are not interpreted. Links are active only for `http`,
`https`, and `mailto` destinations. Malformed or unsupported destinations remain plain
text, and oversized formatted events fall back to plain text. The implementation uses no
Markdown package or external browser resources. See the [browser interface guide](docs/web-interface.md).

## Verification

- The complete offline suite passed: 898 tests.
- The standard-library audit passed; no third-party runtime modules are required.
- Browser checks covered formatted output, unsafe markup, links, and narrow-screen code
  block scrolling.
- Safari behavior, Pyto background behavior, and iOS lifecycle handling still need
  acceptance on a real device.

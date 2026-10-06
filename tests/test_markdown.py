"""Tests for the small safe Markdown tree used by the browser UI."""

from __future__ import annotations

import json
import unittest

from harness.markdown import parse_markdown


def _walk(nodes):
    for node in nodes:
        yield node
        if node[0] in ("paragraph", "heading", "strong", "em", "link"):
            children = node[-1] if node[0] in ("heading", "link") else node[1]
            if isinstance(children, list):
                yield from _walk(children)
        elif node[0] == "list":
            for item in node[3]:
                yield from _walk(item)
        elif node[0] == "quote":
            yield from _walk(node[1])


class TestMarkdownParser(unittest.TestCase):
    def test_common_block_and_inline_formatting(self) -> None:
        blocks = parse_markdown(
            "# A heading\n\nA **bold** and *italic* paragraph with `inline code` and "
            "[a link](https://example.com/path).\n\n> A quoted line\n\n- first\n- second\n\n1. one\n2. two"
        )

        self.assertEqual([block[0] for block in blocks], ["heading", "paragraph", "quote", "list", "list"])
        nodes = list(_walk(blocks))
        self.assertTrue(any(node[0] == "strong" for node in nodes))
        self.assertTrue(any(node[0] == "em" for node in nodes))
        self.assertTrue(any(node[0] == "code" and node[1] == "inline code" for node in nodes))
        self.assertTrue(any(node[0] == "link" and node[1] == "https://example.com/path" for node in nodes))
        self.assertEqual(blocks[3][1:3], ["ul", 1])
        self.assertEqual(blocks[4][1:3], ["ol", 1])
        json.dumps(blocks)

    def test_nested_lists_and_ordered_start_values_are_preserved(self) -> None:
        blocks = parse_markdown("3. outer\n   - nested\n     2) deeper\n4. next")
        self.assertEqual(blocks[0][0:3], ["list", "ol", 3])
        outer_item = blocks[0][3][0]
        self.assertEqual(outer_item[0][1][0][1], "outer")
        nested = outer_item[1]
        self.assertEqual(nested[0:3], ["list", "ul", 1])
        deep = nested[3][0][1]
        self.assertEqual(deep[0:3], ["list", "ol", 2])
        self.assertEqual(blocks[0][3][1][0][1][0][1], "next")

    def test_fenced_code_preserves_whitespace_and_line_endings(self) -> None:
        source = "```python\n  print('hello')  \n\tsecond line\n\n```"
        blocks = parse_markdown(source)
        self.assertEqual(blocks, [["code_block", "  print('hello')  \n\tsecond line\n\n"]])

    def test_raw_html_and_script_markup_remain_text(self) -> None:
        markup = '<img src=x onerror="alert(1)"><script>alert("x")</script>'
        blocks = parse_markdown(markup)
        self.assertEqual(blocks, [["paragraph", [["text", markup]]]])
        self.assertNotIn("html", json.dumps(blocks).lower())
        image = "![remote image](https://example.com/image.png)"
        self.assertEqual(parse_markdown(image), [["paragraph", [["text", image]]]])

    def test_unsafe_link_schemes_are_retained_without_a_destination(self) -> None:
        blocks = parse_markdown(
            "[script](javascript:alert(1)) [data](data:text/html,x) [file](file:///etc/passwd) "
            "[safe](mailto:help@example.com)"
        )
        links = [node for node in _walk(blocks) if node[0] == "link"]
        self.assertEqual([node[1] for node in links], [None, None, None, "mailto:help@example.com"])

    def test_malformed_markdown_stays_readable(self) -> None:
        source = "**unclosed emphasis and [unfinished](javascript:alert(1)"
        blocks = parse_markdown(source)
        self.assertEqual(blocks, [["paragraph", [["text", source]]]])
        self.assertEqual(parse_markdown("```\n  code  \n"), [["code_block", "  code  \n"]])

    def test_empty_and_deep_input_are_safe(self) -> None:
        self.assertEqual(parse_markdown(""), [])
        source = "*" * 40 + "word" + "*" * 40
        self.assertTrue(parse_markdown(source))


if __name__ == "__main__":
    unittest.main()

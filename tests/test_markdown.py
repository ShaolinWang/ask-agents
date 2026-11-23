import unittest

from ask_agents.markdown import BOLD, CODE, DIM, ITALIC, LINK, render_markdown, span_width


def plain(rows):
    return ["".join(text for text, _ in line).rstrip() for line in rows]


class MarkdownTest(unittest.TestCase):
    def test_renders_blocks_and_inline_styles(self):
        rows = render_markdown(
            "## 标题\n"
            "**粗体**、*斜体*、\x60a_b\x60\n"
            "- [x] 检查\n"
            "1. 步骤\n"
            "> 引用\n"
            "[示例](https://example.com)",
            40,
        )
        self.assertEqual(rows[0], [("标题", BOLD)])
        self.assertIn(("粗体", BOLD), rows[1])
        self.assertIn(("斜体", ITALIC), rows[1])
        self.assertIn(("a_b", CODE), rows[1])
        self.assertEqual(plain(rows[2:]), ["☑ 检查", "1. 步骤", "│ 引用", "示例 (https://example.com)"])
        self.assertEqual(rows[5][0], ("示例", LINK))

    def test_streamed_code_keeps_its_markdown_symbols_literal(self):
        partial = "\x60\x60\x60python\n**literal**\n# comment"
        partial_rows = render_markdown(partial, 24)
        self.assertEqual(plain(partial_rows), ["│ python", "│ **literal**", "│ # comment"])
        self.assertIn(("**literal**", 0), partial_rows[1])

        completed = partial + "\n\x60\x60\x60\n**after**"
        self.assertEqual(render_markdown(completed, 24)[-1], [("after", BOLD)])

    def test_wide_table_wraps_or_stacks_to_fit(self):
        markdown = "| Item | Detail | Result |\n| --- | --- | --- |\n| A | x\\|y | 很长的结果 |"
        wide = render_markdown(markdown, 44)
        self.assertIn("┼", plain(wide)[1])
        self.assertIn("x|y", plain(wide)[2])
        self.assertTrue(all(span_width(line) <= 44 for line in wide))
        narrow = render_markdown(markdown, 16)
        self.assertEqual(plain(narrow), ["Item · A", "Detail · x|y", "Result · 很长的", "结果"])
        self.assertTrue(all(span_width(line) <= 16 for line in narrow))

    def test_wrapped_list_does_not_repeat_the_bullet(self):
        rows = render_markdown("- first-second", 9)
        self.assertEqual(plain(rows), ["• first-s", "  econd"])
        self.assertEqual(rows[0][0], ("• ", DIM))

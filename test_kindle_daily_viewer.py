#!/usr/bin/env python3
"""Regression tests for kindle_daily_viewer.

Pure stdlib (unittest) — matches the app's zero-dependency constraint. Each test
pins a bug fixed after the 2026-07-09 Codex architecture review; the test name
maps to the finding it guards.

Run: python3 test_kindle_daily_viewer.py
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_module(vault):
    """Import kindle_daily_viewer with KDV_VAULT pointed at a temp vault."""
    os.environ["KDV_VAULT"] = vault
    os.environ["KDV_CONFIG_DIR"] = os.path.join(vault, ".config")  # avoid real ~/.config
    spec = importlib.util.spec_from_file_location(
        "kdv_under_test", os.path.join(HERE, "kindle_daily_viewer.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class MarkdownListTests(unittest.TestCase):
    """Finding #8 — ordered lists emitted invalid/unclosed HTML (<ol> closed by </ul>)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_ordered_list_is_closed_with_ol(self):
        html = self.m.markdown_to_html("1. one\n2. two\n")
        self.assertIn("<ol>", html)
        self.assertIn("</ol>", html)
        self.assertEqual(html.count("<ol>"), html.count("</ol>"))
        self.assertNotIn("</ul>", html)  # ordered list must never be closed by </ul>

    def test_ordered_list_not_closed_by_ul(self):
        html = self.m.markdown_to_html("1. one\n\nafter\n")
        # Between the <ol> and the paragraph there must be a matching </ol>, no stray </ul>.
        self.assertIn("</ol>", html)
        ol_close = html.index("</ol>")
        para = html.index("<p>after</p>")
        self.assertLess(ol_close, para)

    def test_unordered_list_still_balanced(self):
        html = self.m.markdown_to_html("- a\n- b\n")
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))
        self.assertEqual(html.count("<ul>"), 1)

    def test_nested_unordered_balanced(self):
        html = self.m.markdown_to_html("- a\n    - b\n- c\n")
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))

    def test_switch_ul_to_ol_closes_correctly(self):
        html = self.m.markdown_to_html("- bullet\n1. number\n")
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))
        self.assertEqual(html.count("<ol>"), html.count("</ol>"))


class NestedFenceTests(unittest.TestCase):
    """A 5-backtick outer fence must contain inner 3-backtick fences as CONTENT —
    the outer block only closes on a fence at least as long as the opener. Before
    this fix, any ``` line closed the block, so a nested code block (e.g. a review
    written with a five-backtick wrapper) rendered broken: the inner fence ended
    the <pre> early and the rest leaked into normal markdown."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_nested_fence_is_single_block(self):
        # Outer 5-backtick fence wrapping an inner 3-backtick block.
        text = (
            "`````markdown\n"
            "before\n"
            "```ruby\n"
            "x = 1\n"
            "```\n"
            "after\n"
            "`````\n"
        )
        html = self.m.markdown_to_html(text)
        # Exactly one <pre>/</pre> pair — the inner fence did not close the block.
        self.assertEqual(html.count("<pre>"), 1)
        self.assertEqual(html.count("</pre>"), 1)
        # The inner content survives as literal text inside the block.
        self.assertIn("x = 1", html)
        # The inner fence backticks are rendered as content, not consumed as a toggle.
        self.assertIn("```ruby", html)

    def test_plain_triple_fence_still_one_block(self):
        text = "```\ncode\n```\n"
        html = self.m.markdown_to_html(text)
        self.assertEqual(html.count("<pre>"), 1)
        self.assertEqual(html.count("</pre>"), 1)

    def test_diff_mode_bare_fence_content_does_not_break_block(self):
        # In diff mode, blocks are delimited by <pre>/</pre> sentinels; a bare ```
        # context line inside is diff CONTENT and must stay in the block, not toggle it.
        text = "<pre>\n@@ -1 +1 @@\n ```\n context\n</pre>\n"
        html = self.m.markdown_to_html(text, is_diff=True)
        self.assertEqual(html.count("<pre>"), 1)
        self.assertEqual(html.count("</pre>"), 1)
        self.assertNotIn("<p> context</p>", html)  # must not leak out of the block

    def test_longer_inner_does_not_close_shorter_outer_wrongly(self):
        # A 3-backtick block whose content contains a 4-backtick line: the 4-tick
        # line is >= opener so it DOES close — this documents CommonMark behavior
        # (opener length is the floor). Two separate 3-tick blocks is the realistic
        # case; assert the common one stays balanced.
        text = "```\na\n```\n\n```\nb\n```\n"
        html = self.m.markdown_to_html(text)
        self.assertEqual(html.count("<pre>"), html.count("</pre>"))
        self.assertEqual(html.count("<pre>"), 2)


class ReadabilityRenderTests(unittest.TestCase):
    """2026-07-23 Codex UX audit — three verified readability bugs in markdown_to_html:
    h4 fell through to <p>, nested lists double-indented, word-diff rows lost the gutter
    and broke line numbering for every following line."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_h4_renders_as_heading_not_paragraph(self):
        html = self.m.markdown_to_html("#### Important")
        self.assertIn("<h4>Important</h4>", html)
        self.assertNotIn("<p>#### Important</p>", html)

    def test_nested_list_single_level_for_four_space_indent(self):
        # A 4-space child must open exactly ONE nested <ul>, not two (double indent).
        html = self.m.markdown_to_html("- parent\n    - child\n- sibling\n")
        self.assertEqual(html.count("<ul>"), 2)  # outer + one nested, not three
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))

    def test_nested_list_single_level_for_two_space_indent(self):
        html = self.m.markdown_to_html("- parent\n  - child\n- sibling\n")
        self.assertEqual(html.count("<ul>"), 2)
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))

    def test_same_indent_children_are_siblings_not_nested(self):
        # Two children at the SAME indent must share ONE inner <ul> (siblings), not
        # cascade into deeper lists (the indent/2 heuristic over-nested the 2nd child).
        html = self.m.markdown_to_html("- parent\n    - a\n    - b\n- sibling\n")
        self.assertEqual(html.count("<ul>"), 2)  # outer + one shared inner list
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))

    def test_nested_list_is_valid_html_child_inside_parent_li(self):
        # The child <ul> must sit INSIDE the parent <li>, never as a sibling <ul> of a
        # closed <li> (invalid HTML that Kindle's engine mis-indents).
        from html.parser import HTMLParser

        class _V(HTMLParser):
            def __init__(self):
                super().__init__(); self.stack = []; self.bad = False; self.unclosed = None
            def handle_starttag(self, tag, attrs):
                if tag in ("ul", "ol") and self.stack and self.stack[-1] in ("ul", "ol"):
                    self.bad = True  # a list directly inside a list = invalid
                self.stack.append(tag)
            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()

        for md in ("- a\n    - b\n        - c\n- d",
                   "- [ ] p\n    - [ ] a\n    - [x] b\n- [ ] s"):
            v = _V(); v.feed(self.m.markdown_to_html(md, file_path="x.md"))
            self.assertFalse(v.bad, f"invalid ul-in-ul nesting for: {md!r}")
            self.assertEqual(v.stack, [], f"unbalanced tags for: {md!r}")

    def test_ol_nested_in_ul_stays_balanced(self):
        html = self.m.markdown_to_html("- a\n    1. one\n    2. two\n- b\n")
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))
        self.assertEqual(html.count("<ol>"), html.count("</ol>"))
        self.assertEqual(html.count("<ol>"), 1)  # one shared ordered sublist

    def test_word_diff_row_has_gutter_and_advances_line_numbers(self):
        # The context line AFTER a word-diff row must show the correct (advanced) number.
        text = ("<pre>\n@@ -1,3 +1,3 @@\n ctx before\n"
                "The [-quick-]{+fast+} fox\n ctx after\n</pre>")
        html = self.m.markdown_to_html(text, is_diff=True)
        # word-diff changed row carries a gutter (was missing entirely)
        self.assertIn('<span class="diff-gut">2</span>', html)
        # following context is line 3, not stuck at 2 (the pre-fix bug)
        self.assertIn('<span class="diff-gut">3</span>', html)
        # the changed WORDS are still marked with non-color cues
        self.assertIn('class="wd-del"', html)
        self.assertIn('class="wd-add"', html)


class ImageAndAccessibilityTests(unittest.TestCase):
    """2026-07-23 markdown+a11y pass — images rendered as broken '!alt'; the document
    lacked lang/charset/title and semantic landmarks (WCAG basics)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_http_image_renders_with_alt(self):
        html = self.m.markdown_to_html("![a chart](https://x.com/c.png)")
        self.assertIn('<img src="https://x.com/c.png"', html)
        self.assertIn('alt="a chart"', html)  # alt required for a11y

    def test_local_image_falls_back_to_alt_text(self):
        # Vault-local images aren't served — show alt text, never a broken <img> or '!alt'.
        html = self.m.markdown_to_html("![local pic](img.png)")
        self.assertNotIn("<img", html)
        self.assertNotIn("!local pic", html)
        self.assertIn("local pic", html)

    def test_link_still_works_after_image_rule(self):
        html = self.m.markdown_to_html("[text](https://x.com)")
        self.assertIn('<a href="https://x.com">text</a>', html)
        self.assertNotIn("<img", html)

    def test_template_has_accessibility_basics(self):
        for token in ('lang="en"', "charset", "{page_title}", "<nav", "<main"):
            self.assertIn(token, self.m.HTML_TEMPLATE)

    def test_login_page_has_accessibility_basics(self):
        for token in ('lang="en"', "charset", "<title", "<label"):
            self.assertIn(token, self.m.LOGIN_PAGE)


class FrontmatterTests(unittest.TestCase):
    """Finding #5 — frontmatter split matched any '---' substring, corrupting line offsets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_no_frontmatter(self):
        inner, lines, body = self.m.split_frontmatter("# Title\nbody\n")
        self.assertEqual(inner, "")
        self.assertEqual(lines, 0)
        self.assertEqual(body, "# Title\nbody\n")

    def test_simple_frontmatter(self):
        raw = "---\ntitle: X\n---\n\nBody line\n"
        inner, lines, body = self.m.split_frontmatter(raw)
        self.assertEqual(inner, "title: X")
        self.assertTrue(body.startswith("Body line"))
        # Line offset must equal the count of leading lines removed, so a checkbox on
        # "Body line" resolves to its true source line.
        self.assertEqual(lines, raw.split("Body line")[0].count("\n"))

    def test_horizontal_rule_not_mistaken_for_close(self):
        # A body horizontal rule ('---') must NOT be treated as the frontmatter close.
        raw = "---\ntitle: X\n---\n\nintro\n\n---\n\nafter rule\n"
        inner, lines, body = self.m.split_frontmatter(raw)
        self.assertEqual(inner, "title: X")
        self.assertIn("intro", body)
        self.assertIn("after rule", body)

    def test_dashes_inside_yaml_value(self):
        raw = "---\ndesc: a --- b\nother: y\n---\nbody\n"
        inner, lines, body = self.m.split_frontmatter(raw)
        self.assertIn("desc: a --- b", inner)
        self.assertIn("other: y", inner)
        self.assertEqual(body.strip(), "body")

    def test_unterminated_frontmatter_is_noop(self):
        raw = "---\ntitle: X\nno close here\n"
        inner, lines, body = self.m.split_frontmatter(raw)
        self.assertEqual(inner, "")
        self.assertEqual(lines, 0)
        self.assertEqual(body, raw)

    def test_indented_dashes_do_not_close(self):
        # An indented '---' inside a YAML block scalar must NOT close frontmatter.
        raw = "---\nnote: |\n  line one\n  ---\n  line two\nkey: val\n---\nbody\n"
        inner, lines, body = self.m.split_frontmatter(raw)
        self.assertIn("key: val", inner)      # field after the indented --- stayed in FM
        self.assertEqual(body.strip(), "body")


class GitExTests(unittest.TestCase):
    """Findings #2/#4 — git failures rendered as success; untracked files omitted."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_git_ex_reports_failure_on_non_repo(self):
        non_repo = tempfile.mkdtemp()
        ok, out, err = self.m._git_ex(non_repo, "status", "--short")
        self.assertFalse(ok)
        self.assertTrue(err)  # non-empty error message

    def test_git_ex_success_on_real_repo(self):
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        ok, out, err = self.m._git_ex(repo, "status", "--short")
        self.assertTrue(ok)

    def test_git_legacy_returns_empty_on_failure(self):
        non_repo = tempfile.mkdtemp()
        self.assertEqual(self.m._git(non_repo, "status"), "")

    def test_review_reports_git_error_not_no_changes(self):
        # repo=vault where the vault ISN'T a git repo → must NOT say "No changes".
        md = self.m.get_review_view(repo_param="vault", scope="local")
        self.assertNotIn("No changes in this scope", md)
        self.assertIn("git error", md)

    def _init_repo(self):
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "init"],
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}, check=True)
        self.m.WORKTREE_DIR = os.path.dirname(repo)  # so _review_repo_from_param resolves it
        return repo

    def test_review_lists_untracked_file(self):
        repo = self._init_repo()
        with open(os.path.join(repo, "brand_new.py"), "w") as f:
            f.write("print('unique_marker_123')\n")
        # Single-file view must render the actual CONTENT of the untracked file,
        # not just list it. This is the bug the first pass missed: diff --no-index
        # exits 1, and the diff text was dropped.
        md = self.m.get_review_view(repo_param=os.path.basename(repo), scope="local",
                                    file_only="brand_new.py")
        self.assertIn("brand_new.py", md)
        self.assertIn("unique_marker_123", md)  # the content, not just the name

    def test_first_commit_diff_via_root_fallback(self):
        # A repo whose only commit is the ROOT: `git diff sha~1..sha` fails (no parent),
        # so _file_diff_chunk must recover via `diff-tree --root`. Regression guard for a
        # bug where `continue` on git failure skipped that fallback.
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        with open(os.path.join(repo, "first.txt"), "w") as f:
            f.write("root_commit_marker_789\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", repo, "add", "first.txt"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "root"], env=env, check=True)
        self.m.WORKTREE_DIR = os.path.dirname(repo)
        md = self.m.get_review_view(repo_param=os.path.basename(repo), scope="last",
                                    file_only="first.txt")
        self.assertIn("root_commit_marker_789", md)

    def test_review_lists_untracked_file_in_new_dir(self):
        repo = self._init_repo()
        os.makedirs(os.path.join(repo, "newdir"))
        with open(os.path.join(repo, "newdir", "deep.py"), "w") as f:
            f.write("print('deep_marker_456')\n")
        # -uall must list the file individually (not collapse to 'newdir/').
        md = self.m.get_review_view(repo_param=os.path.basename(repo), scope="local")
        self.assertIn("newdir/deep.py", md)


class TemplateInjectionTests(unittest.TestCase):
    """Finding #11 — note content containing a placeholder token got replaced.

    Verified structurally: {content} must be the LAST replace() in do_GET so a note
    body containing '{nav_button}' survives verbatim. We assert on source order.
    """

    def test_content_substituted_last(self):
        with open(os.path.join(HERE, "kindle_daily_viewer.py")) as fh:
            src = fh.read()
        # find the replace-chain block
        idx_content = src.index('.replace("{content}", content)')
        idx_nav = src.index('.replace("{nav_button}", nav_button)')
        self.assertLess(idx_nav, idx_content, "{content} must be replaced after chrome tokens")


class PathSafetyTests(unittest.TestCase):
    """Finding #1 — bare file= paths skipped the .md/hidden-part policy."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "log"), exist_ok=True)
        cls.m = _load_module(cls.tmp)

    def test_safe_vault_path_rejects_non_md(self):
        self.assertIsNone(self.m._safe_vault_path("secret.txt"))

    def test_safe_vault_path_rejects_hidden(self):
        self.assertIsNone(self.m._safe_vault_path(".obsidian/workspace.json"))

    def test_safe_vault_path_rejects_traversal(self):
        self.assertIsNone(self.m._safe_vault_path("../../etc/passwd"))

    def test_safe_vault_path_allows_md(self):
        p = self.m._safe_vault_path("log/2026-07-09.md")
        self.assertIsNotNone(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)

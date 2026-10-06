from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_desk import library


class LibraryTests(unittest.TestCase):
    def test_lists_safe_files_and_reads_document(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Guides").mkdir()
            (root / "Guides" / "welcome.html").write_text("<title>Welcome Guide</title><h1>Hello</h1>")
            (root / ".hidden.md").write_text("# Hidden")
            (root / "ignore.txt").write_text("No")
            files = library.list_documents(root)["files"]
            self.assertEqual([(f["path"], f["title"]) for f in files],
                             [("Guides/welcome.html", "Welcome Guide")])
            kind, content = library.read_document("Guides/welcome.html", root)
            self.assertEqual(kind, "html")
            self.assertIn(b"<h1>Hello</h1>", content)

    def test_blocks_traversal_and_symlinks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "Library"
            root.mkdir()
            secret = Path(tmp) / "secret.html"
            secret.write_text("private")
            (root / "link.html").symlink_to(secret)
            self.assertEqual(library.list_documents(root)["files"], [])
            for name in ("../secret.html", "link.html", "/secret.html", ".hidden.md"):
                with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                    library.read_document(name, root)


if __name__ == "__main__":
    unittest.main()

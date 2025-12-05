"""
Simple File-Search preparation and chat tool for Google GenAI.

Usage:
  - `python rag.py prepare --docs ./DOCSDocuments --store-name my-docs`
  - `python rag.py ask "What can you tell me about Robert Graves" --store-name my-docs`

This script will:
  - upload all files from a folder to the Files API
  - create a File Search store (or reuse an existing one)
  - import uploaded files into the File Search store
  - provide a chat-like helper that uses the FileSearch tool to query the LLM

"""
import argparse
import json
import os
import unicodedata
import uuid
import re
import sys
import io
import time
import unicodedata
import uuid
import shutil
import tempfile
from pathlib import Path
from typing import List

from google import genai
from google.genai import types


def safe_print(*args, **kwargs):
    """Print safely even if stdout encoding is ASCII and args contain non-ascii chars."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_args = []
        for a in args:
            s = str(a)
            try:
                s.encode(encoding)
                safe_args.append(s)
            except UnicodeEncodeError:
                safe_args.append(s.encode(encoding, errors="replace").decode(encoding))
        print(*safe_args, **kwargs)


class FileSearchTool:
    def __init__(self, client: genai.Client, docs_dir: str, store_display_name: str = "docs-file-search"):
        self.client = client
        self.docs_dir = Path(docs_dir).expanduser().resolve()
        self.store_display_name = store_display_name
        self.state_file = Path(".file_index.json")
        self.state = self._load_state()

    def _load_state(self):
        if self.state_file.exists():
            try:
                # Read explicitly with utf-8 so non-ascii characters aren't lost
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_state(self):
        # Write JSON using utf-8 and don't escape non-ascii characters
        self.state_file.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _list_docs(self) -> List[Path]:
        if not self.docs_dir.exists():
            raise FileNotFoundError(f"Docs dir not found: {self.docs_dir}")
        files = [p for p in self.docs_dir.rglob("*") if p.is_file()]
        return files

    def _ascii_safe(self, s: str) -> str:
        # Transliterate accents and remove non-ascii, then slugify to lowercase alnum + dashes
        # keep file extension (so name.txt -> safe-name.txt)
        base, ext = os.path.splitext(s)
        normalized = unicodedata.normalize("NFKD", base)
        ascii_base = normalized.encode("ascii", "ignore").decode("ascii")
        # Lowercase and replace non-alphanumeric characters with dashes
        slug = re.sub(r"[^a-z0-9]+", "-", ascii_base.lower())
        slug = slug.strip("-")
        if not slug:
            slug = f"file-{uuid.uuid4().hex}"
        # Enforce max 40 chars to satisfy resource name rules
        if len(slug) > 40:
            slug = slug[:40].rstrip("-")
        # We must avoid dots in resource name (API restriction), so do not include extension
        return slug

    def create_or_get_file_search_store(self):
        # Try to see if store exists matching display name, otherwise create
        stores = list(self.client.file_search_stores.list())
        safe_display = self._ascii_safe(self.store_display_name)
        prefer_utf8 = getattr(self, "prefer_utf8", False)
        for s in stores:
            if getattr(s, "display_name", None) == self.store_display_name or getattr(s, "display_name", None) == safe_display:
                return s
        # Try to use the original display name (utf-8) when prefer_utf8 is True; fall back to ASCII-safe
        if prefer_utf8:
            try:
                return self.client.file_search_stores.create(config={"display_name": self.store_display_name})
            except Exception:
                safe_print("Falling back to ASCII-safe store display name due to exception when using UTF-8 names")
                return self.client.file_search_stores.create(config={"display_name": safe_display})
        store = self.client.file_search_stores.create(config={"display_name": safe_display})
        return store

    def upload_file(self, file_path: Path):
        def try_upload_with_name(name: str):
            # Attempt to upload with a specific name; return resource on success
            try:
                # Copy to temporary file with ASCII-safe filename to avoid httpx header encoding issues
                upload_path = str(file_path)
                created_temp = False
                try:
                    has_non_ascii = not file_path.name.isascii()
                except AttributeError:
                    # Python < 3.7 fallback
                    has_non_ascii = any(ord(c) >= 128 for c in file_path.name)

                if has_non_ascii:
                    temp_dir = Path(tempfile.gettempdir())
                    temp_path = temp_dir / name
                    shutil.copy2(file_path, temp_path)
                    upload_path = str(temp_path)
                    created_temp = True

                import mimetypes
                mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                res = self.client.files.upload(file=upload_path, config={"name": name, "mime_type": mime_type})
                return res
            except Exception as e:
                # Propagate exception so caller can fallback
                raise
            finally:
                # Remove temp file if created
                try:
                    if 'created_temp' in locals() and created_temp and temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
        # Skip if file already uploaded and the path exists in state
        key = str(file_path.resolve())
        if self.state.get(key, {}).get("file_id"):
            return self.state[key]

        safe_print(f"Uploading {file_path.name}")
        # Prefer an ascii-safe name by default to avoid header encoding errors
        safe_name = self._ascii_safe(file_path.name)
        # If prefer_utf8 is allowed, try uploading with original filename first and fallback
        prefer_utf8 = getattr(self, "prefer_utf8", False)
        resource = None
        if prefer_utf8:
            try:
                resource = try_upload_with_name(file_path.name)
            except Exception:
                # If upload fails (e.g., header encoding error), fallback to ascii safe
                safe_print("Falling back to ASCII-safe filename for upload due to exception when using UTF-8 names")
                resource = try_upload_with_name(safe_name)
        else:
            resource = try_upload_with_name(safe_name)
        file_info = {"file_id": resource.name, "file_name": resource.name, "original_file_name": file_path.name, "safe_file_name": safe_name}
        self.state[key] = file_info
        self._save_state()
        return file_info

    def import_file_to_store(self, store_name: str, file_name: str):
        if not file_name:
            safe_print(f"Skipping import: missing file_name for store {store_name}")
            return None
        # Normalize to a files/<id> form
        file_ref = file_name
        if not file_ref.startswith("files/"):
            file_ref = f"files/{file_ref}"

        if not self._is_valid_file_id(file_ref):
            safe_print(f"Skipping import: invalid file id format: {file_ref}")
            return None
        safe_print(f"Importing {file_ref} into {store_name}")
        try:
            op = self.client.file_search_stores.import_file(file_search_store_name=store_name, file_name=file_ref)
        except Exception as e:
            # Display friendly context with exception details and skip
            msg = str(e)
            safe_print(f"Failed to import {file_ref} into {store_name}:", msg)
            if "INVALID_ARGUMENT" in msg or "invalid argument" in msg.lower():
                safe_print("Hint: This likely means the file id is invalid or stale. Run `python rag.py prepare --clean-state-only` to remove stale ids.")
            if "PERMISSION_DENIED" in msg or "permission_denied" in msg.lower():
                safe_print("Hint: The file id may not exist or you do not have permission to access it. Check credentials and file ownership.")
            return None
        # Wait for import
        while not op.done:
            time.sleep(2)
            op = self.client.operations.get(op)
        return op

    def _is_valid_file_id(self, file_ref: str) -> bool:
        # Accept 'files/<id>' format where <id> is lowercase alnum/dash with max 40 chars
        if not isinstance(file_ref, str):
            return False
        if not file_ref.startswith("files/"):
            return False
        file_id = file_ref.split("/", 1)[1]
        if not file_id:
            return False
        if len(file_id) > 40:
            return False
        # Only allow lowercase letters, digits, and dashes
        return bool(re.match(r"^[a-z0-9-]+$", file_id))

    def prepare(self):
        safe_print("Preparing file search store and importing files...")
        # Ensure store exists
        store = self.create_or_get_file_search_store()
        store_name = store.name
        files = self._list_docs()
        if not files:
            safe_print("No files found in docs directory.")
            return store
        # Upload and import each file (failures don't abort the entire operation)
        # Remove invalid state entries automatically
        self.clean_state()
        for f in files:
            try:
                info = self.upload_file(f)
            except Exception as e:
                safe_print(f"Failed to upload {f.name}:", e)
                continue
            if not info or not info.get("file_id"):
                safe_print(f"Skipping import for {f.name}: no file_id from upload")
                continue
            try:
                op = self.import_file_to_store(store_name, info["file_id"]) 
                if op is None:
                    safe_print(f"Import skipped for: {f.name}")
            except Exception as e:
                safe_print(f"Failed importing {f.name}:", e)
                continue
        safe_print("All files uploaded and imported")
        return store

    def ask(self, store_name: str, prompt: str, model: str = "gemini-2.5-flash") -> str:
        # Use the model with a file search tool
        safe_print("Sending prompt to LLM with FileSearch tool...")

        response = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        file_search=types.FileSearch(file_search_store_names=[store_name])
                    )
                ]
            ),
        )
        text = None
        try:
            text = response.text
        except Exception:
            # fallback to structured (don't escape non-ascii chars)
            text = json.dumps(response, default=str, indent=2, ensure_ascii=False)
        return text

    def clean_state(self):
        """Remove invalid or clearly-broken entries from state file based on format.
        This helps avoid attempted imports of fake or stale IDs that will fail.
        """
        changed = False
        for k, v in list(self.state.items()):
            file_id = v.get("file_id")
            if not file_id or not self._is_valid_file_id(file_id):
                safe_print(f"Removing invalid state entry: {k} -> {file_id}")
                del self.state[k]
                changed = True
        if changed:
            self._save_state()


def build_client() -> genai.Client:
    # The google.genai.Client will pick up env vars or default credentials depending
    # on the installed SDK and authentication. If you prefer explicit key, set
    # GOOGLE_API_KEY environment variable before running the script.
    # Configure stdout to use UTF-8 so printing non-ascii characters doesn't raise
    try:
        # Python 3.7+ supports TextIOWrapper.reconfigure
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        # Fallback to wrapping the stdout buffer
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            # Last resort: ignore
            pass

    # Prefer environment-based credentials; do not hardcode API keys in source
    api_key = "AIzaSyBuIGvtBxE0lE4a-Rf3asRfG8QflqZZbPk"  # Example key; replace with your own or set env var
    if api_key:
        return genai.Client(api_key=api_key)
    return genai.Client()


def main():
    parser = argparse.ArgumentParser(prog="rag.py")
    subparsers = parser.add_subparsers(dest="cmd")

    # prepare command
    prepare = subparsers.add_parser("prepare", help="Upload and import files in the docs folder to a File Search store")
    prepare.add_argument("--docs", default="DOCSDocuments", help="Path to folder with files to upload")
    prepare.add_argument("--store-name", default="docs-file-search", help="Display name for file search store")
    prepare.add_argument("--utf8-names", action="store_true", default=False, help="Attempt to use UTF-8 filenames/display names directly (fall back to ASCII-safe if API rejects)")
    prepare.add_argument("--clean-state-only", action="store_true", default=False, help="Only run a state cleanup and exit (remove invalid file ids from .file_index.json)")

    # ask command
    ask = subparsers.add_parser("ask", help="Ask a question and consult the File Search store")
    ask.add_argument("query", help="Prompt to send to the model")
    ask.add_argument("--store-name", default="docs-file-search", help="Store display name (use prepare to create)")
    ask.add_argument("--model", default="gemini-2.5-flash", help="Model to use for generation")

    # delete-store command
    delete_s = subparsers.add_parser("delete-store", help="Delete a File Search store and optionally the local state file")
    delete_s.add_argument("--store-name", default="docs-file-search", help="Display name for file search store to delete")
    delete_s.add_argument("--yes", action="store_true", default=False, help="Confirm deletion without prompting")
    delete_s.add_argument("--delete-state", action="store_true", default=False, help="Also remove local .file_index.json state")
    delete_s.add_argument("--force", action="store_true", default=False, help="Delete all documents in the store before deleting the store itself")

    args = parser.parse_args()
    try:
        client = build_client()
    except Exception as e:
        safe_print('Failed to create GenAI client:', e)
        safe_print('Ensure GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS environment variables are set.')
        return
    tool = FileSearchTool(client=client, docs_dir=args.docs if hasattr(args, "docs") else "DOCSDocuments", store_display_name=args.store_name)
    # Set prefer_utf8 based on CLI flag
    setattr(tool, "prefer_utf8", getattr(args, "utf8_names", False))

    if args.cmd == "prepare":
        if getattr(args, "clean_state_only", False):
            tool.clean_state()
            safe_print("State cleanup finished.")
            return
        store = tool.prepare()
        print(f"Store created / used: {store.name} (display: {store.display_name})")
    elif args.cmd == "ask":
        # Find store by display name
        stores = list(client.file_search_stores.list())
        store = None
        for s in stores:
            if s.display_name == args.store_name:
                store = s
                break
        if not store:
            print("Store not found. Create/import it first using `prepare`.")
            return
        answer = tool.ask(store.name, args.query, model=args.model)
        print("--- LLM Response ---")
        print(answer)
    elif args.cmd == "delete-store":
        stores = list(client.file_search_stores.list())
        store = None
        for s in stores:
            if s.display_name == args.store_name or s.display_name == getattr(args, 'store_name', None):
                store = s
                break
        if not store:
            safe_print("Store not found:", args.store_name)
            return
        if not args.yes:
            confirm = input(f"Delete store {store.display_name} (id: {store.name})? This is irreversible. (y/N): ")
            if confirm.lower() != "y":
                safe_print("Aborting store delete")
                return
        try:
            if args.force:
                safe_print(f"Listing documents to delete in {store.display_name} (id: {store.name})")
                # List documents and delete each
                try:
                    docs = client.file_search_stores.documents.list(parent=store.name)
                    count = 0
                    for d in docs:
                        try:
                            client.file_search_stores.documents.delete(name=d.name, config=types.DeleteDocumentConfig(force=True))
                            count += 1
                        except Exception as ex:
                            safe_print(f"Failed deleting document {d.name}:", ex)
                    safe_print(f"Deleted {count} documents from store")
                except Exception as e:
                    safe_print(f"Failed to list/delete documents in store: {e}")
            client.file_search_stores.delete(name=store.name)
            safe_print(f"Deleted store {store.display_name} (id: {store.name})")
        except Exception as e:
            safe_print("Failed to delete store:", e)
        if args.delete_state:
            try:
                if Path('.file_index.json').exists():
                    Path('.file_index.json').unlink()
                    safe_print("Deleted local state .file_index.json")
                else:
                    safe_print("Local state file not present")
            except Exception as e:
                safe_print("Failed to delete local state:", e)
    else:
        parser.print_help()
        stores = list(client.file_search_stores.list())
        store = None
        for s in stores:
            if s.display_name == args.store_name or s.display_name == getattr(args, 'store_name', None):
                store = s
                break
        if not store:
            safe_print("Store not found:", args.store_name)
            return
        if not args.yes:
            confirm = input(f"Delete store {store.display_name} (id: {store.name})? This is irreversible. (y/N): ")
            if confirm.lower() != "y":
                safe_print("Aborting store delete")
                return
        try:
            client.file_search_stores.delete(name=store.name)
            safe_print(f"Deleted store {store.display_name} (id: {store.name})")
        except Exception as e:
            safe_print("Failed to delete store:", e)
        if args.delete_state:
            try:
                if Path('.file_index.json').exists():
                    Path('.file_index.json').unlink()
                    safe_print("Deleted local state .file_index.json")
                else:
                    safe_print("Local state file not present")
            except Exception as e:
                safe_print("Failed to delete local state:", e)


if __name__ == "__main__":
    main()

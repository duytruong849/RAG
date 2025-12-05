# File Search + Chat Tool (Google GenAI)

This repository includes a small tool that uploads and imports all files in a `DOCSDocuments/` folder to a Google GenAI File Search store and provides a chat-like interface for asking questions that reference those files.

Prerequisites
- Python 3.10+
- A Google GenAI API client and credentials. The `google-genai` client SDK will look for application credentials/bearer tokens in the environment; two common ways to provide credentials are:

  1. Set `GOOGLE_API_KEY` to an API key (the client SDK uses it when supported).
  2. Set `GOOGLE_APPLICATION_CREDENTIALS` to point to a JSON service account key file.

  Follow the GenAI SDK docs for your exact environment setup if you need a different method.

Install
```
pip install -r requirements.txt
```

Usage
1. Prepare the file-search store and import files:
```
python rag.py prepare \
  --docs ./DOCSDocuments \
  --store-name my-docs-store
```
Additional prepare options:
```
# Attempt to use UTF-8 filenames & display names, with fallback to ASCII-safe names
python rag.py prepare --docs ./DOCSDocuments --store-name my-docs-store --utf8-names

# Only clean the local `.file_index.json` state (remove invalid/stale file IDs)
python rag.py prepare --clean-state-only --docs ./DOCSDocuments --store-name my-docs-store
```

2. Ask a question (after prepare finished):
```
python rag.py ask "Tell me about Robert Graves" \
  --store-name my-docs-store
```

Notes
- This code uses the Google GenAI `Files API` and `File Search Store` to import files and then the `FileSearch` tool to enrich queries issued to the model.
- The script saves a small `.file_index.json` to avoid re-uploading files unnecessarily.

Encoding / Non-ASCII filenames
- Windows console or older Python builds can default to ASCII and cause errors when printing or writing filenames with non-ASCII characters (e.g. Vietnamese characters like `à`). You can avoid these by:

  1. Running PowerShell with UTF-8 page: `chcp 65001` before running the script.
  2. Setting the environment flag before running Python (Windows):
     ```powershell
     $env:PYTHONUTF8=1
     python rag.py prepare --docs ./DOCSDocuments
     ```
  3. Use PowerShell 7+ or enable UTF-8 in your terminal settings.

The script itself attempts to reconfigure stdout to UTF-8 and writes state files using UTF-8 to improve compatibility.

License: MIT

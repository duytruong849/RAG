"""
Simple example of using `rag.py` programmatically.
"""
from google import genai
from rag import FileSearchTool, build_client


def run_example():
    client = build_client()
    tool = FileSearchTool(client=client, docs_dir="DOCSDocuments", store_display_name="my-docs-store")
    store = tool.prepare()
    print("Store used:", store.name)
    answer = tool.ask(store.name, "Tell me about Robert Graves")
    print(answer)


if __name__ == '__main__':
    run_example()

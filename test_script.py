from rag import FileSearchTool

t = FileSearchTool(client=None, docs_dir='DOCSDocuments')
print('List docs:')
print([p.name for p in t._list_docs()])

# Set a state entry and save
key = str((t.docs_dir / 'test_ấn.txt').resolve())
t.state[key] = {'file_id': 'fake-id', 'file_name': 'test_ấn.txt'}
# Save state
t._save_state()
print('Saved file_index.json contents:')
print(open('.file_index.json','r', encoding='utf-8').read())

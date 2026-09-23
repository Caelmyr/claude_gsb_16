"""通过 Flask 测试客户端验证完整 API 链路（临时数据目录）"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP_DIR = tempfile.mkdtemp(prefix='kg_api_test_')
import backend.utils.config as config
for attr in ['DOCUMENTS_DIR', 'TRIPLES_DIR', 'GRAPH_DIR', 'SESSIONS_DIR']:
    sub = os.path.join(TMP_DIR, attr.lower().replace('_dir', ''))
    os.makedirs(sub, exist_ok=True)
    setattr(config, attr, sub)

# app.py 在导入时即使用这些常量，需要同时 patch 其模块内引用
import backend.app as app_module
app_module.DOCUMENTS_DIR = config.DOCUMENTS_DIR
app_module.TRIPLES_DIR = config.TRIPLES_DIR

client = app_module.app.test_client()

DOC = """北京大学是中国著名的高等学府，简称北大。北大培养了大量优秀人才。
中国科学院（简称中科院）是中国自然科学的最高学术机构，中科院成立于1949年。
中科院与北京大学在北京开展深度合作。
人工智能（Artificial Intelligence，简称AI）技术发展迅速，AI已经广泛应用于各个行业。
"""

# 上传
resp = client.post('/api/documents/upload', data={
    'file': (io.BytesIO(DOC.encode('utf-8')), 'synonym_test.txt')
}, content_type='multipart/form-data')
assert resp.status_code == 200, resp.data
doc_id = resp.get_json()['document']['id']
print('上传成功 doc_id =', doc_id)

# 解析
resp = client.post(f'/api/documents/{doc_id}/parse')
data = resp.get_json()
assert data['success']
print(f"实体数={data['entities_count']} 关系数={data['relations_count']} "
      f"消歧合并数={data['merged_count']}")
print('合并分组:')
for g in data['merge_groups']:
    print(f"  {g['canonical']} ({g['type']}) <= {', '.join(g['aliases'])}")

entity_names = {e['text'] for e in data['entities']}
assert '北京大学' in entity_names and '北大' not in entity_names
assert '中国科学院' in entity_names and '中科院' not in entity_names
assert '人工智能' in entity_names and 'AI' not in entity_names

# 图谱数据
graph = client.get('/api/graph/data').get_json()
labels = {n['label'] for n in graph['nodes']}
assert '北大' not in labels and '中科院' not in labels and 'AI' not in labels
pku = next(n for n in graph['nodes'] if n['label'] == '北京大学')
assert '北大' in pku['aliases']
print(f"\n图谱节点数={len(graph['nodes'])} 边数={len(graph['links'])}")
print('北京大学节点别名:', pku['aliases'])

# 别名取实体详情
r = client.get('/api/entities/' + __import__('urllib.parse', fromlist=['quote']).quote('北大')).get_json()
assert r['found'] and r['entity']['text'] == '北京大学'
print(f"\nGET /api/entities/北大 -> {r['entity']['text']}, 关系数={len(r['relations'])}")

# 别名子图
r = client.get('/api/entities/' + __import__('urllib.parse', fromlist=['quote']).quote('中科院')).get_json()
assert r['found'] and r['entity']['text'] == '中国科学院'
print(f"GET /api/entities/中科院 -> {r['entity']['text']}, 关系数={len(r['relations'])}")

# 关键词搜索（别名命中）
r = client.post('/api/graph/query', json={'query': 'AI'}).get_json()
names = [e['text'] for e in r['entities']]
assert '人工智能' in names, names
print(f"POST /api/graph/query 'AI' -> {names}")

# 问答链路：用别名提问
r = client.post('/api/qa/ask', json={'question': '北大是什么？'}).get_json()
print(f"\n问答(北大): {r['answer'].splitlines()[0]}")
assert '北京大学' in r['answer']

# 统计
stats = client.get('/api/graph/statistics').get_json()
print('\n统计:', json.dumps(stats, ensure_ascii=False))

# 二次解析（增量 + 幂等）
resp2 = client.post(f'/api/documents/{doc_id}/parse')
d2 = resp2.get_json()
graph2 = client.get('/api/graph/data').get_json()
pku2 = next(n for n in graph2['nodes'] if n['label'] == '北京大学')
assert sum(1 for n in graph2['nodes'] if n['label'] == '北大') == 0
print(f"二次解析后节点数={len(graph2['nodes'])}（无重复/孤立别名节点）, 北大count={pku2['count']}")

shutil.rmtree(TMP_DIR, ignore_errors=True)
print('\nAPI 全链路验证通过 ✓')

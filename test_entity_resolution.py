"""实体消歧合并功能验证脚本（使用临时数据目录，不影响正式数据）"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 在导入存储前重定向图谱目录
TMP_DIR = tempfile.mkdtemp(prefix='kg_test_')
import backend.utils.config as config
config.GRAPH_DIR = os.path.join(TMP_DIR, 'graph')
os.makedirs(config.GRAPH_DIR, exist_ok=True)

from backend.nlp.entity_resolver import EntityResolver
from backend.nlp.pipeline import NLPPipeline
from backend.graph.storage import GraphStorage

failures = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' -> {detail}' if detail and not cond else ''))
    if not cond:
        failures.append(name)


# ----------------------------------------------------------------------
# 1. 文本简称声明提取
# ----------------------------------------------------------------------
print('\n===== 1. 同义词发现（词典 + 文本模式） =====')
resolver = EntityResolver()
pairs = resolver.extract_aliases(
    '北京大学（以下简称“北大”）是著名高校。中国科学院（简称中科院）位于北京。'
    '麻省理工学院（MIT）和斯坦福大学的研究人员合作。'
    '人工智能（Artificial Intelligence，简称AI）是计算机科学的分支。'
)
pair_set = set(pairs)
check('提取“北大”简称声明', ('北京大学', '北大') in pair_set, str(pair_set))
check('提取“中科院”简称声明', ('中国科学院', '中科院') in pair_set, str(pair_set))
check('提取 MIT 英文缩写', ('麻省理工学院', 'MIT') in pair_set, str(pair_set))
check('提取 AI 英文缩写', ('人工智能', 'AI') in pair_set, str(pair_set))
# 不应把“Artificial Intelligence”整句当成别名（长度超过缩写模式）
check('不误提取英文全称为缩写', ('人工智能', 'Artificial Intelligence') not in pair_set)

# 反例：普通括号说明不应误判
r2 = EntityResolver()
p2 = r2.extract_aliases('公司在某年（具体时间待定）完成融资。')
check('普通括号说明不误判为别名', p2 == [], str(p2))

# ----------------------------------------------------------------------
# 2. Pipeline 端到端合并
# ----------------------------------------------------------------------
print('\n===== 2. 解析阶段实体合并与关系改写 =====')
text = (
    '北大是中国著名的高等学府。北京大学培养了大量人才。'
    '中科院成立于1949年。中国科学院是中国自然科学的最高学术机构。'
    '北大和中科院都位于北京。'
    '人工智能是计算机科学的一个分支，AI技术近年来发展迅速。'
)
pipeline = NLPPipeline()
result = pipeline.process(text)

ent_by_text = {e['text']: e for e in result['entities']}
check('只保留规范实体“北京大学”', '北京大学' in ent_by_text, str(list(ent_by_text)))
check('“北大”不再作为独立节点', '北大' not in ent_by_text)
check('只保留规范实体“中国科学院”', '中国科学院' in ent_by_text)
check('“中科院”不再作为独立节点', '中科院' not in ent_by_text)
check('只保留规范实体“人工智能”', '人工智能' in ent_by_text)
check('“AI”不再作为独立节点', 'AI' not in ent_by_text)

aliases_pku = ent_by_text.get('北京大学', {}).get('aliases', [])
check('“北大”保留为“北京大学”的别名', '北大' in aliases_pku, str(aliases_pku))
aliases_cas = ent_by_text.get('中国科学院', {}).get('aliases', [])
check('“中科院”保留为“中国科学院”的别名', '中科院' in aliases_cas, str(aliases_cas))
aliases_ai = ent_by_text.get('人工智能', {}).get('aliases', [])
check('“AI”保留为“人工智能”的别名', 'AI' in aliases_ai, str(aliases_ai))

check('merged_count 统计正确', result['merged_count'] >= 3, str(result['merged_count']))
check('merge_groups 包含合并分组',
      any(g['canonical'] == '北京大学' and '北大' in g['aliases']
          for g in result['merge_groups']),
      str(result['merge_groups']))

# 关系中不应再出现别名
all_rel_names = set()
for r in result['relations']:
    all_rel_names.add(r['subject'])
    all_rel_names.add(r['object'])
check('关系主语/宾语均为规范名称',
      not ({'北大', '中科院', 'AI'} & all_rel_names), str(all_rel_names))

# 同类型安全：跨类型不应误合
check('不同类型实体未被误合',
      all(e['text'] != '中国' for e in result['entities'] if e['type'] == 'ORG'))

# ----------------------------------------------------------------------
# 3. 存储层：写入重定向 + 别名搜索
# ----------------------------------------------------------------------
print('\n===== 3. 图谱存储：别名索引 / 搜索定位 =====')
storage = GraphStorage()

# 模拟两个文档分别写入全称和简称（写入即归并）
storage.add_entity('北京大学', 'ORG', {'doc_id': 'd1'})
storage.add_entity('北大', 'ORG', {'doc_id': 'd2'})
storage.add_relation('北大', 'ORG', '位于', '北京', 'LOCATION')

all_e = {e['text']: e for e in storage.get_all_entities()}
check('存储层只存在一个“北京大学”节点', '北京大学' in all_e and '北大' not in all_e, str(list(all_e)))
check('出现次数合并为2', all_e['北京大学']['count'] == 2, str(all_e['北京大学'].get('count')))
check('节点别名包含“北大”', '北大' in all_e['北京大学'].get('aliases', []),
      str(all_e['北京大学'].get('aliases')))

# 用别名取实体 / 关系
e = storage.get_entity('北大')
check('get_entity("北大") 定位到北京大学', e is not None and e['text'] == '北京大学')
rels = storage.get_entity_relations('北大')
check('别名可查到归并后的关系', len(rels) == 1 and rels[0]['subject'] == '北京大学',
      json.dumps(rels, ensure_ascii=False))

# 搜索
hits = storage.search_entities('北大')
check('搜索“北大”命中规范节点', len(hits) == 1 and hits[0]['text'] == '北京大学',
      str([(h['text'], h.get('matched_alias')) for h in hits]))
hits2 = storage.search_entities('北京')
check('搜索“北京”同时可命中相关节点',
      any(h['text'] == '北京大学' for h in hits2) and any(h['text'] == '北京' for h in hits2),
      str([h['text'] for h in hits2]))

# 英文缩写搜索
hits3 = storage.search_entities('PKU')
check('搜索“PKU”可定位到北京大学', any(h['text'] == '北京大学' for h in hits3))

# 图谱可视化数据
graph = storage.get_graph_data()
node = next(n for n in graph['nodes'] if n['label'] == '北京大学')
check('图节点携带别名', '北大' in node.get('aliases', []))
check('图中不存在“北大”孤立节点', not any(n['label'] == '北大' for n in graph['nodes']))
link_labels = [(l['source'], l['target'], l['label']) for l in graph['links']]
check('图边连接到合并后的规范节点',
      any(lbl == '位于' for _, _, lbl in link_labels))

# 持久化检查
files = os.listdir(config.GRAPH_DIR)
check('aliases.json 已持久化', 'aliases.json' in files, str(files))
with open(os.path.join(config.GRAPH_DIR, 'aliases.json'), encoding='utf-8') as f:
    idx = json.load(f)
check('别名索引记录 北大->北京大学', '北大' in idx['entries'])

# ----------------------------------------------------------------------
# 4. 存量数据迁移：旧库里已有“北大”“北京大学”两个节点
# ----------------------------------------------------------------------
print('\n===== 4. 存量同义节点启动迁移 =====')
shutil.rmtree(config.GRAPH_DIR, ignore_errors=True)
os.makedirs(config.GRAPH_DIR, exist_ok=True)

# 手工构造“消歧功能上线前”的分片数据：两个分散节点 + 分散关系
org_shard = {
    'entities': {
        '北大': {'id': 'ORG_0', 'text': '北大', 'type': 'ORG', 'properties': {'doc_id': 'old1'}, 'count': 1},
        '北京大学': {'id': 'ORG_1', 'text': '北京大学', 'type': 'ORG', 'properties': {'doc_id': 'old2'}, 'count': 1},
    },
    'relations': [
        {'subject': '北大', 'subject_type': 'ORG', 'predicate': '位于',
         'object': '北京市', 'object_type': 'LOCATION', 'properties': {}},
        {'subject': '北京大学', 'subject_type': 'ORG', 'predicate': '属于',
         'object': '中国', 'object_type': 'LOCATION', 'properties': {}},
        {'subject': '北大', 'subject_type': 'ORG', 'predicate': '自环误配',
         'object': '北京大学', 'object_type': 'ORG', 'properties': {}},
    ]
}
loc_shard = {
    'entities': {
        '北京市': {'id': 'LOCATION_0', 'text': '北京市', 'type': 'LOCATION', 'properties': {}, 'count': 1}
    },
    'relations': []
}
with open(os.path.join(config.GRAPH_DIR, 'org.json'), 'w', encoding='utf-8') as f:
    json.dump(org_shard, f, ensure_ascii=False)
with open(os.path.join(config.GRAPH_DIR, 'location.json'), 'w', encoding='utf-8') as f:
    json.dump(loc_shard, f, ensure_ascii=False)

storage2 = GraphStorage()  # 初始化即触发迁移
all_e2 = {e['text']: e for e in storage2.get_all_entities()}
check('迁移后只剩“北京大学”', '北京大学' in all_e2 and '北大' not in all_e2, str(list(all_e2)))
check('北京市迁移为“北京”', '北京' in all_e2 and '北京市' not in all_e2, str(list(all_e2)))
check('计数合并', all_e2['北京大学']['count'] == 2)
check('属性合并保留两个doc_id',
      all_e2['北京大学']['properties'].get('doc_id') in ('old1', 'old2'))
check('迁移后别名完整',
      set(all_e2['北京大学']['aliases']) >= {'北大', 'PKU', 'Peking University'},
      str(all_e2['北京大学']['aliases']))

rels2 = storage2.get_entity_relations('北京大学')
preds = sorted(r['predicate'] for r in rels2)
check('两条关系全部归集到规范实体', '位于' in preds and '属于' in preds, str(preds))
check('合并产生的自环关系被删除', '自环误配' not in preds, str(preds))
located = [r for r in rels2 if r['predicate'] == '位于'][0]
check('关系宾语也被规范化（北京市->北京）', located['object'] == '北京', str(located))

# 幂等：再次初始化不应出错或重复计数
storage3 = GraphStorage()
all_e3 = {e['text']: e for e in storage3.get_all_entities()}
check('迁移幂等', '北大' not in all_e3 and all_e3['北京大学']['count'] == 2,
      str(all_e3['北京大学']))

# ----------------------------------------------------------------------
# 5. 查询引擎别名透明
# ----------------------------------------------------------------------
print('\n===== 5. 查询 / 问答检索别名透明 =====')
from backend.graph.query import GraphQuery
from backend.qa.retriever import SemanticRetriever

q = GraphQuery(storage2)
res = q.query_entity('北大')
check('query_entity 支持别名', res['found'] and res['entity']['text'] == '北京大学')
check('query_entity 返回别名列表', '北大' in res['entity']['aliases'])
sub = q.query_subgraph('PKU', depth=1)
sub_labels = {n['label'] for n in sub['nodes']}
check('query_subgraph 支持英文缩写', '北京大学' in sub_labels, str(sub_labels))
kw = q.query_keyword('中科院')
check('关键词搜索别名可解析（无结果时安全返回）', 'entities' in kw)

retriever = SemanticRetriever(storage2)
ctx = retriever.retrieve_context('北大在哪里', max_entities=5)
check('问答检索可通过别名找到实体', '北京大学' in ctx, ctx)

shutil.rmtree(TMP_DIR, ignore_errors=True)

print('\n========================================')
if failures:
    print(f'{len(failures)} 项失败: {failures}')
    sys.exit(1)
print('全部测试通过 ✓')

"""Compile bounded, parameterized library search rules into item predicates."""
from __future__ import annotations

from .common import require


SCALAR = {
    'title': 'i.title', 'author': 'i.authors', 'doi': 'i.doi',
    'journal': 'i.container', 'year': 'i.year',
    'abstract': "coalesce(json_extract(i.data,'$.abstract'),'')",
}
RELATED = {
    'tag': 'SELECT 1 FROM item_tags it JOIN tags t ON t.id=it.tag_id WHERE it.item_id=i.id AND {test}',
    'note': 'SELECT 1 FROM notes n WHERE n.item_id=i.id AND {test}',
    'annotation': 'SELECT 1 FROM annotations a JOIN attachments at ON at.id=a.attachment_id WHERE at.item_id=i.id AND {test}',
    'pdf': 'SELECT 1 FROM document_chunks c WHERE c.item_id=i.id AND c.attachment_id IS NOT NULL AND {test}',
}
RELATED_TEXT = {
    'tag': 't.name', 'note': "(n.title || ' ' || n.content)",
    'annotation': "(coalesce(json_extract(a.data,'$.quote'),'') || ' ' || coalesce(json_extract(a.data,'$.comment'),''))",
    'pdf': 'c.text',
}


def compile_rule(rule):
    require(isinstance(rule, dict), '高级检索条件应为对象')
    leaves = [0]

    def walk(node, depth):
        require(depth <= 4 and isinstance(node, dict), '高级检索最多嵌套四层')
        if 'conditions' in node:
            op, children = node.get('op'), node['conditions']
            require(op in ('all', 'any') and isinstance(children, list) and 1 <= len(children) <= 20,
                    '条件组必须包含1–20项，并选择全部或任一匹配')
            compiled = [walk(child, depth + 1) for child in children]
            return '(' + (' AND ' if op == 'all' else ' OR ').join(sql for sql, _ in compiled) + ')', [p for _, args in compiled for p in args]
        leaves[0] += 1
        require(leaves[0] <= 40, '高级检索最多40个条件')
        field, operator = node.get('field'), node.get('operator')
        require(field in SCALAR or field in RELATED, '不支持的高级检索字段')
        require(operator in ('contains', 'equals', 'isEmpty', 'isNotEmpty'), '不支持的高级检索关系')
        value = str(node.get('value') or '').strip()
        if operator in ('contains', 'equals'):
            require(0 < len(value) <= 200, '检索词长度应为1–200个字符')
        value = value.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        if field in SCALAR:
            column = SCALAR[field]
            if operator == 'isEmpty': return f"trim({column})=''", []
            if operator == 'isNotEmpty': return f"trim({column})<>''", []
            if operator == 'equals': return f'lower({column})=?', [value]
            return f"lower({column}) LIKE ? ESCAPE '\\'", ['%' + value + '%']
        text = RELATED_TEXT[field]
        if operator in ('isEmpty', 'isNotEmpty'):
            exists = RELATED[field].format(test='1=1')
            return ('NOT EXISTS' if operator == 'isEmpty' else 'EXISTS') + f' ({exists})', []
        test = f'lower({text})=?' if operator == 'equals' else f"lower({text}) LIKE ? ESCAPE '\\'"
        sql = RELATED[field].format(test=test)
        return f'EXISTS ({sql})', [value if operator == 'equals' else '%' + value + '%']

    return walk(rule, 0)

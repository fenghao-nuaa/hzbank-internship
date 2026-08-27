"""Write a self-contained, regulator-friendly HTML graph report."""

import html
import json
from pathlib import Path
from typing import Any


class HTMLVisualizer:
    def export(self, graph: dict[str, list[dict[str, Any]]], path: str | Path) -> Path:
        nodes = graph["entities"]
        edges = graph["relationships"]
        node_rows = "".join(
            f"<tr><td>{html.escape(node['id'])}</td><td>{html.escape(node['type'])}</td>"
            f"<td><pre>{html.escape(json.dumps(node['properties'], ensure_ascii=False, sort_keys=True, default=str))}</pre></td></tr>"
            for node in nodes
        )
        edge_rows = "".join(
            f"<tr><td>{html.escape(edge['source'])}</td><td>{html.escape(edge['type'])}</td>"
            f"<td>{html.escape(edge['target'])}</td></tr>"
            for edge in edges
        )
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AuditGraph Report</title>
<style>body{{font:14px system-ui;margin:2rem;color:#172033}}h1{{color:#1d4ed8}}table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}th,td{{border:1px solid #ccd3df;padding:.55rem;text-align:left;vertical-align:top}}th{{background:#eef3ff}}pre{{margin:0;white-space:pre-wrap}}.summary{{display:flex;gap:1rem}}.card{{padding:1rem;background:#f7f9fc;border-radius:.6rem}}</style></head>
<body><h1>AuditGraph Decision Report</h1><div class="summary"><div class="card">Nodes: {len(nodes)}</div><div class="card">Edges: {len(edges)}</div></div>
<h2>Nodes</h2><table><thead><tr><th>ID</th><th>Type</th><th>Properties</th></tr></thead><tbody>{node_rows}</tbody></table>
<h2>Relationships</h2><table><thead><tr><th>Source</th><th>Type</th><th>Target</th></tr></thead><tbody>{edge_rows}</tbody></table></body></html>"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8")
        return target

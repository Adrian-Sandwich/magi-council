"""Vuelca memory.db a Node_visualizer/graphs/memory.kgraph.json, pasando por
finalize() del contrato (dedupe, tira edges colgantes, recalcula counts) en
vez de reimplementar esa validación acá.

La ruta al visor sale de `settings` (env `NODE_VISUALIZER_DIR`): estaba escrita
literal acá, así que mover o clonar Node_visualizer rompía la exportación con
un ImportError sin explicación.
"""

import json
import sys
from pathlib import Path

import db
import settings

NODE_VISUALIZER = settings.NODE_VISUALIZER
OUT_PATH = settings.KGRAPH_OUT
TOOLTIP_MAX = 200

if not (NODE_VISUALIZER / "kgraph_contract.py").exists():
    # El visor es opcional: sin él el grafo sigue sirviendo al consejo. Salir
    # con error acá hacía fallar refresh.sh (y la tarea programada) en cada
    # corrida aunque la ingesta hubiera andado bien, y tapaba fallos reales.
    print(
        f"[export_kgraph] omitido: no encuentro kgraph_contract.py en {NODE_VISUALIZER}. "
        f"Apuntá NODE_VISUALIZER_DIR al checkout de Node_visualizer si querés el visor 3D.",
        file=sys.stderr,
    )
    raise SystemExit(0)

sys.path.insert(0, str(NODE_VISUALIZER))
from kgraph_contract import finalize  # noqa: E402

DOMAIN_COLORS = {
    "project": "#66d9ff",
    "claude_session": "#7b8cff",
    "kimi_session": "#b07bff",
    "debate_thread": "#ff7bd5",
    "decision": "#ff9f1c",
    "doc": "#ffd166",
    "file": "#ef476f",
    "code": "#06d6a0",
}


def main() -> None:
    conn = db.connect()
    node_rows = conn.execute("SELECT id, label, tag, domain, size, tooltip, props FROM nodes").fetchall()
    edge_rows = conn.execute(
        "SELECT from_id, to_id, type, label_forward, label_backward, weight FROM edges"
    ).fetchall()
    conn.close()

    nodes = []
    per_domain = {}
    for id_, label, tag, domain, size, tooltip, props in node_rows:
        nodes.append({
            "id": id_,
            "label": (label or id_)[:60],
            "tag": tag,
            "domain": domain,
            "size": size or 10,
            "tooltip": (tooltip or "")[:TOOLTIP_MAX],
            "props": json.loads(props or "{}"),
        })
        per_domain[domain] = per_domain.get(domain, 0) + 1

    edges = [
        {
            "from": from_id, "to": to_id, "type": type_,
            "label_forward": label_forward, "label_backward": label_backward,
            "weight": weight,
        }
        for from_id, to_id, type_, label_forward, label_backward, weight in edge_rows
    ]

    obj = {
        "meta": {"domain_colors": DOMAIN_COLORS, "title": "Grafo de memoria", "source": "memory-graph"},
        "nodes": nodes,
        "edges": edges,
    }
    finalize(obj)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(obj))

    print(f"[export_kgraph] {len(obj['nodes'])} nodos, {len(obj['edges'])} edges -> {OUT_PATH}")
    for dom, count in sorted(per_domain.items(), key=lambda kv: -kv[1]):
        print(f"  {dom}: {count}")


if __name__ == "__main__":
    main()

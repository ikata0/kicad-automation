"""Check a floorplan before routing: can every port of the candidate cells reach the source?

Fitting by area says nothing about whether a port is walled in by its neighbours. This is a coarse
maze search on a grid per layer, from a source region outwards. Cells and keep-outs block the layers
they name, grown by half a track and a clearance; the board edge and its cut-outs block all. A
through via needs room on every layer. It answers reachability, one port at a time: ports that can
each get out may still not all fit through one gap, so routing and DRC stay the judges.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

from .. import env, report
from .board import Board
from .geometry import inside, point_seg

DEFAULT_EDGE = 0.5
VIA_COST = 2.0            # mm: a path may be this much longer to save a via


class _Shape:
    """A named obstacle on some layers: a box, or a polygon."""

    def __init__(self, name: str, layers: set[str], box=None, polygon=None):
        self.name, self.layers = name, set(layers)
        self.polygon = [tuple(p) for p in polygon] if polygon else None
        if self.polygon:
            xs, ys = zip(*self.polygon)
            box = (min(xs), min(ys), max(xs), max(ys))
        self.box = tuple(box)

    def covers(self, x: float, y: float, margin: float) -> bool:
        x0, y0, x1, y1 = self.box
        if not (x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin):
            return False
        if self.polygon is None:
            return True
        if inside(self.polygon, (x, y)):
            return True
        return any(point_seg((x, y), a, b) <= margin for a, b in zip(self.polygon, self.polygon[1:] + self.polygon[:1]))


def _box_of(spec: dict, where: str, key: str = "box") -> tuple[float, float, float, float]:
    box = spec.get(key)
    if not (isinstance(box, (list, tuple)) and len(box) == 4):
        raise env.KatError(f"{where}: {key} is [x0, y0, x1, y1] in mm")
    x0, y0, x1, y1 = (float(v) for v in box)
    if x1 <= x0 or y1 <= y0:
        raise env.KatError(f"{where}: {key} {list(box)} is empty")
    return x0, y0, x1, y1


def _layers(value, every: list[str], where: str) -> set[str]:
    if value is None:
        return set(every)
    names = {value} if isinstance(value, str) else set(value)
    bad = sorted(names - set(every))
    if bad:
        raise env.KatError(f"{where}: no layer {', '.join(bad)}: {', '.join(every)}")
    return names


def _read(spec: dict | Path | str) -> dict:
    if isinstance(spec, dict):
        return dict(spec)
    path = Path(spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise env.KatError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise env.KatError(f"{path.name}: expected an object")
    return {**data, "_dir": str(path.parent)}      # files it names are beside it


def access(spec: dict | Path | str) -> dict:
    """See ``kat.pcb.port_access``."""
    spec = _read(spec)
    base = Path(spec["_base"]) if spec.get("_base") else Path.cwd()
    board = None
    if spec.get("board"):
        path = Path(spec["board"])
        path = path if path.is_absolute() else base / path
        report.touch(path)
        board = Board(path)
    every = board.copper_layers() if board else list(spec.get("board_layers") or spec.get("layers") or ["F.Cu", "B.Cu"])
    allowed = _layers(spec.get("layers"), every, "layers")
    grid = float(spec.get("grid", 0.5))
    if grid <= 0:
        raise env.KatError("grid must be above 0 mm")
    track, clearance = float(spec.get("track", 0.2)), float(spec.get("clearance", 0.2))
    via = float(spec.get("via", 0.6))
    vias = bool(spec.get("vias", True))
    margin, via_margin = track / 2 + clearance, via / 2 + clearance
    # the area, its edge and cut-outs
    if board is not None:
        loops = board.outline_loops()
        if not loops:
            raise env.KatError(f"{path.name} has no closed outline")
        edge_poly, cutouts = loops[0], loops[1:]
        xs, ys = zip(*edge_poly)
        area = (min(xs), min(ys), max(xs), max(ys))
        edge_cl = board.board_minimums().get("min_copper_edge_clearance", DEFAULT_EDGE) or 0.0
    else:
        area = _box_of(spec, "floorplan", "outline")
        edge_poly, cutouts = None, []
        edge_cl = float(spec.get("edge_clearance", DEFAULT_EDGE))
    shapes: list[_Shape] = [_Shape(f"cut-out {k + 1}", set(every), polygon=c) for k, c in enumerate(cutouts)]
    warnings: list[str] = []
    # keep-outs
    for k, q in enumerate(spec.get("keepouts") or [], 1):
        where = f"keepout {q.get('name') or k}"
        layers = _layers(q.get("layers"), every, where)
        if q.get("polygon"):
            shapes.append(_Shape(q.get("name") or f"keepout {k}", layers, polygon=q["polygon"]))
        else:
            shapes.append(_Shape(q.get("name") or f"keepout {k}", layers, box=_box_of(q, where)))
    # what the board already has: keep-outs for tracks, and pads of parts
    if board is not None:
        from ..sexpr import find
        from .specctra import _zone_layers, _zone_polygon
        for z in board.zones():
            keep = find(z, "keepout")
            if keep is None:
                continue
            rule = {str(c[0]): str(c[1]) for c in keep[1:] if isinstance(c, list)}
            if rule.get("tracks") == "not_allowed":
                name = find(z, "name")
                shapes.append(_Shape(str(name[1]) if name else "keep-out", set(_zone_layers(z)) & set(every),
                                     polygon=_zone_polygon(z)))
        if spec.get("parts", True):
            for f in board.footprints():
                for p in f.pads():
                    q = p.outline
                    shapes.append(_Shape(f.ref, {l for l in every if p.on(l)},
                                         box=(min(a for a, _ in q), min(b for _, b in q), max(a for a, _ in q), max(b for _, b in q))))
    # the cells
    cells = []
    listed = spec.get("cells") or []
    if isinstance(listed, str):          # a site list (kat pcb stamp --sites), one block for all
        from .stamping import read_sites
        file = Path(listed) if Path(listed).is_absolute() or not spec.get("_dir") else Path(spec["_dir"]) / listed
        listed = []
        for r in read_sites(file, need_instance=False):
            cell = {"block": r["block"] or spec.get("block"), "at": r["at"], "rot": r["rot"],
                    "instance": r["instance"], "side": r["side"], **({"name": r["name"]} if r["name"] else {})}
            listed.append(cell)
            if r["twin"]:
                listed.append({**cell, "instance": r["twin"],
                               "side": "top" if (r["side"] or "top") == "bottom" else "bottom"})
    for k, c in enumerate(listed, 1):
        name = c.get("name") or c.get("instance") or f"cell {k}"
        ports = c.get("ports")
        if c.get("block"):
            if board is None:
                raise env.KatError(f"cell {name}: a block needs board")
            got = board.stamp_preview(c["block"], tuple(c.get("at", (0, 0))), float(c.get("rot", 0)), c.get("instance"),
                                      c.get("side"), c.get("layer_map"), bool(c.get("allow_asymmetric", False)),
                                      anchor=c.get("anchor"))
            box = tuple(got["outline"])
            ports = [{"name": n, **v} for n, v in got["ports"].items()]
            if not ports:
                warnings.append(f"cell {name}: block {c['block']} has no port points (kat pcb port)")
            warnings += got.get("warnings", [])
            polygon = None
        elif c.get("polygon"):
            box, polygon = None, c["polygon"]
        else:
            box, polygon = _box_of(c, f"cell {name}", "outline"), None
        layers = _layers(c.get("blocked_layers"), every, f"cell {name}")
        shape = _Shape(name, layers, box=box, polygon=polygon)
        cells.append((name, shape, list(ports or [])))
        shapes.append(shape)
    if not cells:
        raise env.KatError("no cells to check")
    src = spec.get("source")
    if not isinstance(src, dict):
        raise env.KatError("source is {box: [x0, y0, x1, y1], layers: [...]}")
    src_box = _box_of(src, "source")
    src_layers = _layers(src.get("layers"), every, "source") & allowed
    if not src_layers:
        raise env.KatError("the source is on no layer routing may use")

    nx, ny = int(math.ceil((area[2] - area[0]) / grid)) + 1, int(math.ceil((area[3] - area[1]) / grid)) + 1
    if nx * ny * len(every) > 8_000_000:
        raise env.KatError(f"grid {grid} mm gives {nx * ny * len(every)} points: use a coarser grid")

    def in_area(x: float, y: float) -> bool:
        need = edge_cl + track / 2
        if edge_poly is None:
            return area[0] + need <= x <= area[2] - need and area[1] + need <= y <= area[3] - need
        return inside(edge_poly, (x, y)) and all(point_seg((x, y), a, b) >= need
                                                 for a, b in zip(edge_poly, edge_poly[1:] + edge_poly[:1]))

    node_x = [area[0] + i * grid for i in range(nx)]
    node_y = [area[1] + j * grid for j in range(ny)]
    free = {l: bytearray(b"\x01") * (nx * ny) for l in every}
    via_free = bytearray(b"\x01") * (nx * ny)
    for j, y in enumerate(node_y):
        for i, x in enumerate(node_x):
            if not in_area(x, y):
                for l in every:
                    free[l][j * nx + i] = 0
                via_free[j * nx + i] = 0

    def stamp(shape: _Shape) -> None:
        reach = max(margin, via_margin)
        i0 = max(0, int(math.floor((shape.box[0] - reach - area[0]) / grid)))
        i1 = min(nx - 1, int(math.ceil((shape.box[2] + reach - area[0]) / grid)))
        j0 = max(0, int(math.floor((shape.box[1] - reach - area[1]) / grid)))
        j1 = min(ny - 1, int(math.ceil((shape.box[3] + reach - area[1]) / grid)))
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                x, y = node_x[i], node_y[j]
                k = j * nx + i
                if shape.covers(x, y, margin):
                    for l in shape.layers:
                        free[l][k] = 0
                if via_free[k] and shape.covers(x, y, via_margin):
                    via_free[k] = 0

    for shape in shapes:
        stamp(shape)
    order = [l for l in every if l in allowed]
    index = {l: n for n, l in enumerate(order)}
    size = nx * ny
    inf = math.inf
    dist = [inf] * (size * len(order))
    hops = [0] * (size * len(order))
    prev = [-1] * (size * len(order))
    heap: list[tuple[float, int]] = []
    starts = 0
    for l in src_layers:
        base_l = index[l] * size
        for j, y in enumerate(node_y):
            if not src_box[1] <= y <= src_box[3]:
                continue
            for i, x in enumerate(node_x):
                if src_box[0] <= x <= src_box[2] and free[l][j * nx + i]:
                    dist[base_l + j * nx + i] = 0.0
                    heap.append((0.0, base_l + j * nx + i))
                    starts += 1
    if not starts:
        raise env.KatError("the source has no free grid point: every point in it is blocked or off the board "
                           f"(grid {grid} mm)")
    heapq.heapify(heap)
    rows = [free[l] for l in order]
    while heap:
        d, s = heapq.heappop(heap)
        if d > dist[s]:
            continue
        layer, k = divmod(s, size)
        j, i = divmod(k, nx)
        row = rows[layer]
        for n, ok in ((k - 1, i > 0), (k + 1, i < nx - 1), (k - nx, j > 0), (k + nx, j < ny - 1)):
            if ok and row[n]:
                t = layer * size + n
                nd = d + grid
                if nd < dist[t]:
                    dist[t], hops[t], prev[t] = nd, hops[s], s
                    heapq.heappush(heap, (nd, t))
        if vias and via_free[k] and len(order) > 1:
            for other in range(len(order)):
                if other != layer and rows[other][k]:
                    t = other * size + k
                    nd = d + VIA_COST
                    if nd < dist[t]:
                        dist[t], hops[t], prev[t] = nd, hops[s] + 1, s
                        heapq.heappush(heap, (nd, t))

    def walled_by(start: tuple[int, int]) -> list[str]:
        """What stands around the free region a blocked port is in."""
        seen = {start}
        todo = [start]
        names = set()
        while todo:
            layer, k = todo.pop()
            j, i = divmod(k, nx)
            x, y = node_x[i], node_y[j]
            for n, ok in ((k - 1, i > 0), (k + 1, i < nx - 1), (k - nx, j > 0), (k + nx, j < ny - 1)):
                if not ok:
                    continue
                if rows[layer][n]:
                    if (layer, n) not in seen:
                        seen.add((layer, n))
                        todo.append((layer, n))
                    continue
                jn, in_ = divmod(n, nx)
                xn, yn = node_x[in_], node_y[jn]
                hit = [s.name for s in shapes if order[layer] in s.layers and s.covers(xn, yn, margin)]
                names.update(hit or ["board edge"])
            if vias and via_free[k]:
                for other in range(len(order)):
                    if other != layer and rows[other][k] and (other, k) not in seen:
                        seen.add((other, k))
                        todo.append((other, k))
        return sorted(names)

    def corners(s: int) -> list[list]:
        out = []
        while s != -1:
            layer, k = divmod(s, size)
            j, i = divmod(k, nx)
            out.append([round(node_x[i], 3), round(node_y[j], 3), order[layer]])
            s = prev[s]
        keep = [p for n, p in enumerate(out) if n in (0, len(out) - 1) or not (
            (out[n - 1][0] == p[0] == out[n + 1][0] or out[n - 1][1] == p[1] == out[n + 1][1])
            and out[n - 1][2] == p[2] == out[n + 1][2])]
        return keep

    results = []
    for name, shape, ports in cells:
        for q in ports:
            port = q.get("name", "?")
            if "at" in q:
                px, py = float(q["at"][0]), float(q["at"][1])
            elif "x" in q and "y" in q:
                px, py = float(q["x"]), float(q["y"])
            else:
                raise env.KatError(f"cell {name} port {port}: give at, or x and y")
            where = _layers(q.get("layers", q.get("layer")), every, f"cell {name} port {port}") & allowed
            row = {"cell": name, "port": port, **({"net": q["net"]} if q.get("net") else {})}
            escape = float(spec.get("escape", margin + 2 * grid))
            reach = int(math.ceil(escape / grid))
            ci, cj = int(round((px - area[0]) / grid)), int(round((py - area[1]) / grid))
            best, near, exits = None, inf, 0
            for l in where:
                base_l = index[l] * size
                for j in range(max(0, cj - reach), min(ny, cj + reach + 1)):
                    for i in range(max(0, ci - reach), min(nx, ci + reach + 1)):
                        k = j * nx + i
                        step = math.hypot(node_x[i] - px, node_y[j] - py)
                        if step > escape or not free[l][k]:
                            continue
                        exits += 1
                        total = dist[base_l + k] + step
                        if total < near:
                            best, near = base_l + k, total
            if not where:
                results.append({**row, "status": "sealed", "why": "the port is on no layer routing may use"})
            elif not exits:
                results.append({**row, "status": "sealed", "why": f"no free point within {escape:g} mm on {', '.join(sorted(where))}"})
            elif best is None:
                start = None
                for l in where:
                    for j in range(max(0, cj - reach), min(ny, cj + reach + 1)):
                        for i in range(max(0, ci - reach), min(nx, ci + reach + 1)):
                            if start is None and free[l][j * nx + i] and math.hypot(node_x[i] - px, node_y[j] - py) <= escape:
                                start = (index[l], j * nx + i)
                results.append({**row, "status": "blocked", "walled_by": walled_by(start)})
            else:
                layer, k = divmod(best, size)
                j, i = divmod(k, nx)
                results.append({**row, "status": "reachable", "length": round(near - hops[best] * VIA_COST, 2), "vias": hops[best],
                                "exit": [round(node_x[i], 3), round(node_y[j], 3), order[layer]],
                                **({"path": corners(best)} if spec.get("path") else {})})
    overlaps = []
    for a in range(len(cells)):
        for b in range(a + 1, len(cells)):
            sa, sb = cells[a][1], cells[b][1]
            common = sa.layers & sb.layers
            if common and sa.box[0] < sb.box[2] and sb.box[0] < sa.box[2] and sa.box[1] < sb.box[3] and sb.box[1] < sa.box[3]:
                overlaps.append({"a": sa.name, "b": sb.name, "layers": sorted(common)})
    outside = [n for n, s, _ in cells if s.box[0] < area[0] or s.box[1] < area[1] or s.box[2] > area[2] or s.box[3] > area[3]]
    counts = {k: sum(r["status"] == k for r in results) for k in ("reachable", "blocked", "sealed")}
    if not results:
        warnings.append("no ports to check")
    return env.tidy({"ok": not (counts["blocked"] or counts["sealed"] or overlaps or outside) and bool(results),
                     "ports": results, "counts": counts, "overlaps": overlaps, "outside": outside, "grid": grid,
                     **({"warnings": warnings} if warnings else {})})

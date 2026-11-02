"""Read-only PipeWire connection map with focused signal-path tracing."""
import json
import logging
import subprocess
import threading
from collections import defaultdict, deque

from gi.repository import Gtk, GLib

LOG = logging.getLogger("generic")


def read_graph():
    """Read actual nodes, ports and directed links from the PipeWire registry."""
    objects = json.loads(subprocess.check_output(["pw-dump"], timeout=5))
    nodes, ports, links = {}, {}, {}
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        kind = obj.get("type", "").rsplit(":", 1)[-1]
        if kind == "Node":
            nodes[obj["id"]] = {
                "name": str(props.get("node.description") or props.get("application.name")
                            or props.get("node.nick") or props.get("node.name") or obj["id"]),
                "class": str(props.get("media.class") or ""),
                "internal": str(props.get("node.name") or ""),
            }
        elif kind == "Port":
            ports[obj["id"]] = {
                "node": props.get("node.id"),
                "name": str(props.get("port.name") or obj["id"]),
            }
    for obj in objects:
        if not obj.get("type", "").endswith(":Link"):
            continue
        info = obj.get("info") or {}
        source = ports.get(info.get("output-port-id"))
        target = ports.get(info.get("input-port-id"))
        if not source or not target:
            continue
        a, b = source["node"], target["node"]
        if a not in nodes or b not in nodes:
            continue
        links.setdefault((a, b), []).append((source["name"], target["name"]))
    return nodes, links


def visible_graph(nodes, links, query="", direction="Both", hide_monitors=True):
    """Select a node and trace all real directed links upstream/downstream.

    Without a query, show all visible links. Intermediate devices retain their
    identity: a node is never duplicated as a separate source and destination.
    """
    excluded = set()
    if hide_monitors:
        excluded = {node_id for node_id, node in nodes.items()
                    if "peak" in node["name"].lower()
                    or "peak" in node["internal"].lower()}
    remaining = {key: value for key, value in links.items()
                 if key[0] not in excluded and key[1] not in excluded}
    q = query.casefold().strip()
    if not q:
        active = {n for pair in remaining for n in pair}
        return {n: nodes[n] for n in active}, remaining

    seeds = {n for n, data in nodes.items() if n not in excluded
             and (q in data["name"].casefold() or q in data["internal"].casefold()
                  or q == str(n))}
    forward, backward = defaultdict(set), defaultdict(set)
    for a, b in remaining:
        forward[a].add(b)
        backward[b].add(a)
    shown = set(seeds)
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        neighbours = set()
        if direction in ("Both", "Downstream"):
            neighbours.update(forward[current])
        if direction in ("Both", "Upstream"):
            neighbours.update(backward[current])
        for neighbour in neighbours - shown:
            shown.add(neighbour)
            queue.append(neighbour)
    return ({n: nodes[n] for n in shown},
            {pair: channels for pair, channels in remaining.items()
             if pair[0] in shown and pair[1] in shown})


def graph_positions(nodes, links):
    """Lay out every node exactly once, in left-to-right signal-flow layers.

    Cycles are kept in one layer if needed rather than expanding indefinitely.
    """
    if not nodes:
        return {}, 900, 450
    outgoing = defaultdict(set)
    incoming = defaultdict(set)
    for a, b in links:
        if a != b:
            outgoing[a].add(b)
            incoming[b].add(a)
    # Kahn ordering; assign remaining cycle members bounded layers.
    indegree = {n: len(incoming[n]) for n in nodes}
    roots = sorted((n for n in nodes if not indegree[n]),
                   key=lambda n: (nodes[n]["name"].casefold(), n))
    pending = deque(roots)
    layers = {n: 0 for n in roots}
    processed = set()
    while pending:
        n = pending.popleft()
        processed.add(n)
        for target in sorted(outgoing[n]):
            layers[target] = max(layers.get(target, 0), layers[n] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    for n in nodes:
        if n not in processed:
            layers[n] = min(4, max((layers.get(p, 0) + 1 for p in incoming[n]
                                     if p in processed), default=0))
    # Allow room for multiple branches of the same signal path.
    columns = defaultdict(list)
    for n in nodes:
        columns[layers[n]].append(n)
    positions = {}
    for layer, node_ids in columns.items():
        for idx, n in enumerate(sorted(node_ids,
                     key=lambda item: (nodes[item]["name"].casefold(), item))):
            positions[n] = (30 + layer * 320, 50 + idx * 70)
    return positions, max(900, (max(columns) + 1) * 320 + 40), max(
        450, max(len(column) for column in columns.values()) * 70 + 110)


class ConnectionsWindow(Gtk.Window):
    """Live view only: never creates or changes PipeWire links."""

    def __init__(self, parent):
        super().__init__(title="PipeWire Connections", transient_for=parent,
                         default_width=1100, default_height=720)
        self.nodes, self.links = {}, {}
        self.shown_nodes, self.shown_links = {}, {}
        self.positions = {}
        self.closed = False
        self.busy = False

        self.search = Gtk.SearchEntry(placeholder_text="Focus on app or device (e.g. SonoToMic)")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self.update_view)

        self.direction = Gtk.ComboBoxText()
        for value in ("Both", "Upstream", "Downstream"):
            self.direction.append_text(value)
        self.direction.set_active(0)
        self.direction.connect("changed", self.update_view)
        self.direction.set_tooltip_text("Show routes leading to/from matching nodes")

        self.hide_peaks = Gtk.CheckButton(label="Hide peak meters")
        self.hide_peaks.set_active(True)
        self.hide_peaks.connect("toggled", self.update_view)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)
        toolbar.set_margin_top(10)
        toolbar.set_margin_bottom(8)
        toolbar.append(self.search)
        toolbar.append(self.direction)
        toolbar.append(self.hide_peaks)

        self.status = Gtk.Label(xalign=0)
        self.status.set_margin_start(12)
        self.status.set_margin_bottom(6)

        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        self.area.set_content_width(900)
        self.area.set_content_height(450)
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.area)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(toolbar)
        root.append(self.status)
        root.append(scroll)
        self.set_child(root)
        self.connect("close-request", self.on_close)
        self.timer = GLib.timeout_add_seconds(2, self.refresh)
        self.refresh()

    def on_close(self, *_):
        self.closed = True
        GLib.source_remove(self.timer)
        return False

    def refresh(self):
        if self.closed:
            return False
        if not self.busy:
            self.busy = True
            threading.Thread(target=self.fetch, daemon=True).start()
        return True

    def fetch(self):
        try:
            data, error = read_graph(), None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            data, error = None, str(exc)
        GLib.idle_add(self.apply_graph, data, error)

    def apply_graph(self, data, error):
        self.busy = False
        if self.closed:
            return False
        if error:
            self.status.set_text("Unable to read PipeWire graph: " + error)
            return False
        self.nodes, self.links = data
        self.update_view()
        return False

    def update_view(self, *_):
        self.shown_nodes, self.shown_links = visible_graph(
            self.nodes, self.links, self.search.get_text(),
            self.direction.get_active_text() or "Both",
            self.hide_peaks.get_active())
        self.positions, width, height = graph_positions(
            self.shown_nodes, self.shown_links)
        self.area.set_content_width(width)
        self.area.set_content_height(height)
        self.status.set_text(
            f"{len(self.shown_nodes)} nodes · {len(self.shown_links)} routes · "
            f"{sum(len(v) for v in self.shown_links.values())} port links "
            "· 2-second refresh · Read-only")
        self.area.queue_draw()

    def draw(self, _area, cr, _width, _height):
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(12)
        # Draw real links, from the output side of a node to the input side
        # of the next node; virtual devices stay visible as intermediates.
        for (a, b), channels in self.shown_links.items():
            x1, y1 = self.positions[a]
            x2, y2 = self.positions[b]
            x1 += 245
            y1 += 20
            y2 += 20
            cr.set_source_rgba(.19, .61, .80, .75)
            cr.set_line_width(1.4 + min(len(channels), 8) * .25)
            cr.move_to(x1, y1)
            if x2 > x1:
                offset = max(25, (x2 - x1) / 2)
                cr.curve_to(x1 + offset, y1, x2 - offset, y2, x2, y2)
            else:
                cr.curve_to(x1 + 40, y1 + 40, x2 - 40, y2 - 40, x2, y2)
            cr.stroke()

        matches = self.search.get_text().casefold().strip()
        for node_id, (x, y) in self.positions.items():
            data = self.shown_nodes[node_id]
            kind = data["class"]
            is_match = matches and (matches in data["name"].casefold()
                                    or matches in data["internal"].casefold())
            if is_match:
                color = (.70, .43, .12)
            elif "Source" in kind:
                color = (.17, .50, .36)
            elif "Sink" in kind:
                color = (.16, .38, .62)
            else:
                color = (.27, .34, .48)
            cr.set_source_rgb(*color)
            cr.rectangle(x, y, 245, 40)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            label = data["name"]
            if len(label) > 30:
                label = label[:27] + "..."
            cr.move_to(x + 9, y + 24)
            cr.show_text(label)
            cr.set_source_rgba(.5, .5, .5, 1)
            cr.set_font_size(10)
            cr.move_to(x + 5, y + 53)
            cr.show_text(f"#{node_id}  {kind}"[:40])
            cr.set_font_size(12)

        if not self.shown_nodes:
            cr.set_source_rgb(.5, .5, .5)
            cr.move_to(30, 100)
            cr.show_text("No matching connections. Try another search.")

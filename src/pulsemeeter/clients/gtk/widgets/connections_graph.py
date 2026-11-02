"""Read-only PipeWire graph viewer for routing diagnostics."""
import json
import logging
import subprocess
import threading

from gi.repository import Gtk, GLib

LOG = logging.getLogger("generic")


def read_graph():
    """Return actual PipeWire node-to-node links, based on pw-dump port IDs."""
    output = subprocess.check_output(["pw-dump"], timeout=5)
    objects = json.loads(output)
    nodes = {}
    ports = {}
    for obj in objects:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        kind = obj.get("type", "").rsplit(":", 1)[-1]
        if kind == "Node":
            nodes[obj["id"]] = {
                "name": props.get("node.description") or props.get("application.name")
                        or props.get("node.nick") or props.get("node.name")
                        or str(obj["id"]),
                "class": props.get("media.class", ""),
            }
        elif kind == "Port":
            ports[obj["id"]] = {
                "node": props.get("node.id"),
                "name": props.get("port.name", str(obj["id"])),
            }

    links = {}
    for obj in objects:
        if not obj.get("type", "").endswith(":Link"):
            continue
        info = obj.get("info") or {}
        out_port = ports.get(info.get("output-port-id"))
        in_port = ports.get(info.get("input-port-id"))
        if not out_port or not in_port:
            continue
        source, target = out_port["node"], in_port["node"]
        if source not in nodes or target not in nodes:
            continue
        key = (source, target)
        links.setdefault(key, []).append((out_port["name"], in_port["name"]))

    return nodes, links


class ConnectionsWindow(Gtk.Window):
    """A non-editable, automatically refreshed overview of PipeWire links."""

    def __init__(self, parent):
        super().__init__(title="PipeWire Connections", transient_for=parent,
                         default_width=900, default_height=640)
        self.nodes = {}
        self.links = {}
        self.busy = False
        self.closed = False
        self.status = Gtk.Label(label="Loading PipeWire connections…", xalign=0)
        self.status.set_margin_start(12)
        self.status.set_margin_end(12)
        self.status.set_margin_top(8)
        self.status.set_margin_bottom(8)

        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self.draw)
        self.area.set_content_width(860)
        self.area.set_content_height(500)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.set_child(self.area)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.append(self.status)
        container.append(scroll)
        self.set_child(container)
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
            data = read_graph()
            GLib.idle_add(self.apply_graph, data, None)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            GLib.idle_add(self.apply_graph, None, str(exc))

    def apply_graph(self, data, error):
        self.busy = False
        if self.closed:
            return False
        if error:
            self.status.set_text("Unable to read PipeWire graph: " + error)
            return False
        self.nodes, self.links = data
        self.status.set_text(
            f"{len(self.links)} routes · {sum(len(v) for v in self.links.values())} port links"
            " · Live snapshot every 2 seconds · Read-only"
        )
        count = len({n for pair in self.links for n in pair})
        self.area.set_content_height(max(420, count * 62 + 90))
        self.area.queue_draw()
        return False

    def draw(self, _area, cr, width, _height):
        # Display only nodes participating in links. Sources and destinations can
        # overlap (virtual nodes), so give every node one stable row per column.
        sources = sorted({s for s, _ in self.links},
                         key=lambda n: self.nodes[n]["name"].casefold())
        targets = sorted({t for _, t in self.links},
                         key=lambda n: self.nodes[n]["name"].casefold())
        left = {node: 60 + i * 62 for i, node in enumerate(sources)}
        right = {node: 60 + i * 62 for i, node in enumerate(targets)}
        height = max(len(left), len(right)) * 62 + 90
        box_width = 260
        right_x = max(340, width - box_width - 24)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(12)

        for (source, target), channels in self.links.items():
            sy, ty = left[source], right[target]
            cr.set_source_rgba(.24, .62, .82, .70)
            cr.set_line_width(1.6 + min(len(channels), 6) * .25)
            cr.move_to(24 + box_width, sy + 17)
            cr.curve_to(right_x - 95, sy + 17, right_x - 95, ty + 17,
                        right_x, ty + 17)
            cr.stroke()

        for positions, x, color in ((left, 24, (.17, .42, .66)),
                                    (right, right_x, (.20, .54, .40))):
            for node_id, y in positions.items():
                cr.set_source_rgb(*color)
                cr.rectangle(x, y, box_width, 34)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                label = self.nodes[node_id]["name"]
                if len(label) > 35:
                    label = label[:32] + "…"
                cr.move_to(x + 9, y + 21)
                cr.show_text(label)

        if not self.links:
            cr.set_source_rgba(.5, .5, .5, 1)
            cr.move_to(28, 90)
            cr.show_text("No active PipeWire links found.")

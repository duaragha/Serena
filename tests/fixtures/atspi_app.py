"""A small GTK app for accessibility tests: python3 atspi_app.py STATE_FILE [other]."""

import json
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

state_path = sys.argv[1]
other = len(sys.argv) > 2 and sys.argv[2] == "other"
state = {"count": 0, "name": "", "subscribe": False, "plan": "Free", "confirmed": False, "typed": ""}


def save():
    with open(state_path, "w", encoding="utf-8") as stream:
        json.dump(state, stream)


def named(widget, name):
    widget.get_accessible().set_name(name)
    return widget


if other:
    # Stands in for the window Raghav is typing in: it keeps focus throughout.
    window = Gtk.Window(title="His other app")
    entry = named(Gtk.Entry(), "His notes")
    entry.connect("changed", lambda e: (state.update(typed=e.get_text()), save()))
    window.add(entry)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window.present()
    save()
    Gtk.main()
    sys.exit(0)

window = Gtk.Window(title="Serena apps test")
window.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
window.add(box)

label = Gtk.Label(label="Count: 0")
box.add(label)

add = Gtk.Button(label="Add one")


def added(_button):
    state["count"] += 1
    label.set_text(f"Count: {state['count']}")
    save()


add.connect("clicked", added)
box.add(add)

name = named(Gtk.Entry(), "Name")
name.connect("changed", lambda e: (state.update(name=e.get_text()), save()))
box.add(name)

subscribe = Gtk.CheckButton(label="Subscribe")
subscribe.connect("toggled", lambda b: (state.update(subscribe=b.get_active()), save()))
box.add(subscribe)

plan = named(Gtk.ComboBoxText(), "Plan")
for item in ("Free", "Pro", "Team"):
    plan.append_text(item)
plan.set_active(0)
plan.connect("changed", lambda c: (state.update(plan=c.get_active_text()), save()))
box.add(plan)

password = named(Gtk.Entry(), "Password")
password.set_visibility(False)
box.add(password)


def open_dialog(_button):
    dialog = Gtk.Dialog(title="Confirm", transient_for=window)
    ok = dialog.add_button("OK", Gtk.ResponseType.OK)

    def confirmed(*_args):
        state["confirmed"] = True
        label.set_text("Confirmed")
        save()
        dialog.destroy()

    ok.connect("clicked", confirmed)
    dialog.show_all()


opener = Gtk.Button(label="Open dialog")
opener.connect("clicked", open_dialog)
box.add(opener)

window.show_all()
save()
GLib.timeout_add_seconds(600, Gtk.main_quit)
Gtk.main()

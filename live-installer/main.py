#!/usr/bin/python3
import os
os.environ['GTK_THEME']="Adwaita"
if not os.path.isfile("installer.py"):
    os.chdir("/usr/lib/live-installer")

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk
from frontend.gtk_interface import InstallerWindow
import sys
import subprocess

sys.path.insert(1, '/usr/lib/live-installer')

gi.require_version('Gtk', '3.0')

def gtk_style():
        style_provider = Gtk.CssProvider()
        style_provider.load_from_path('./resources/theme/gtk.css')

        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

# main entry
if __name__ == "__main__":
    #gtk_style()
    if ("--expert-mode" in sys.argv):
        win = InstallerWindow(expert_mode=True)
    else:
        win = InstallerWindow()
    if ("--fullscreen" in sys.argv):
        win.fullscreen()
    Gtk.main()

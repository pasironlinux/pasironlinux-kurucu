#!/usr/bin/python3

from gi.repository import Gtk, Gdk, GdkPixbuf, GObject, Pango, GLib
from installer import InstallerEngine, Setup, NON_LATIN_KB_LAYOUTS
from dialogs import MessageDialog, QuestionDialog, ErrorDialog, WarningDialog
import timezones
import partitioning
import os
import re
import subprocess
import sys
import threading
import time
import parted
import cairo

import gi
gi.require_version('Gtk', '3.0')


LOADING_ANIMATION = './resources/loading.gif'

# Used as a decorator to run things in the background


def asynchronous(func):
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread
    return wrapper

# Used as a decorator to run things in the main loop, from another thread


def idle(func):
    def wrapper(*args, **kwargs):
        GObject.idle_add(func, *args, **kwargs)
    return wrapper


class WizardPage:

    def __init__(self, help_text, icon, question):
        self.help_text = help_text
        self.icon = icon
        self.question = question


class InstallerWindow:
    # Cancelable timeout for keyboard preview generation, which is
    # quite expensive, so avoid drawing it if only scrolling through
    # the keyboard layout list
    kbd_preview_generation = -1

    def __init__(self, expert_mode=False):

        self.expert_mode = expert_mode

        # disable the screensaver
        os.system("killall cinnamon-screen")

        # build the setup object (where we put all our choices) and the installer
        self.setup = Setup()
        self.installer = InstallerEngine(self.setup)

        self.resource_dir = './resources/'
        glade_file = os.path.join(self.resource_dir, 'interface.ui')
        self.builder = Gtk.Builder()
        self.builder.add_from_file(glade_file)

        # should be set early
        self.done = False
        self.fail = False
        self.paused = False
        self.showing_last_dialog = False

        # load the window object
        self.window = self.builder.get_object("main_window")
        self.window.connect("delete-event", self.quit_cb)

        # wizard pages
        (self.PAGE_WELCOME,
         self.PAGE_LANGUAGE,
         self.PAGE_TIMEZONE,
         self.PAGE_KEYBOARD,
         self.PAGE_USER,
         self.PAGE_TYPE,
         self.PAGE_PARTITIONS,
         self.PAGE_ADVANCED,
         self.PAGE_OVERVIEW,
         self.PAGE_CUSTOMWARNING,
         self.PAGE_CUSTOMPAUSED,
         self.PAGE_INSTALL) = list(range(12))

        # set the button events (wizard_cb)
        self.builder.get_object("button_next").connect(
            "clicked", self.wizard_cb, False)
        self.builder.get_object("button_back").connect(
            "clicked", self.wizard_cb, True)
        self.builder.get_object("button_quit").connect("clicked", self.quit_cb)

        col = Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), pixbuf=2)
        self.builder.get_object("treeview_language_list").append_column(col)
        ren = Gtk.CellRendererText()
        self.language_column = Gtk.TreeViewColumn(("Dil"), ren, text=0)
        self.language_column.set_sort_column_id(0)
        self.language_column.set_expand(True)
        self.language_column.set_resizable(True)
        ren.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.builder.get_object("treeview_language_list").append_column(
            self.language_column)

        ren = Gtk.CellRendererText()
        self.country_column = Gtk.TreeViewColumn(("??lke"), ren, text=1)
        self.country_column.set_sort_column_id(1)
        self.country_column.set_expand(True)
        self.country_column.set_resizable(True)
        ren.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.builder.get_object(
            "treeview_language_list").append_column(self.country_column)

        self.builder.get_object("treeview_language_list").connect(
            "cursor-changed", self.assign_language)

        # build the language list
        self.build_lang_list()

        # build timezones
        timezones.build_timezones(self)

        # type page
        model = Gtk.ListStore(str, str)
        model.set_sort_column_id(0, Gtk.SortType.ASCENDING)
        for disk_path, disk_description in partitioning.get_disks():
            iterator = model.append(
                ("%s (%s)" % (disk_description, disk_path), disk_path))
        self.builder.get_object("combo_disk").set_model(model)
        renderer_text = Gtk.CellRendererText()
        self.builder.get_object("combo_disk").pack_start(renderer_text, True)
        self.builder.get_object("combo_disk").add_attribute(
            renderer_text, "text", 0)

        self.builder.get_object("radio_automated").connect(
            "toggled", self.assign_type_options)
        self.builder.get_object("radio_manual").connect(
            "toggled", self.assign_type_options)
        self.builder.get_object("check_badblocks").connect(
            "toggled", self.assign_type_options)
        self.builder.get_object("combo_disk").connect(
            "changed", self.assign_type_options)

        # partitions
        self.builder.get_object("button_expert").connect(
            "clicked", self.show_customwarning)
        self.builder.get_object("button_edit").connect(
            "clicked", partitioning.manually_edit_partitions)
        self.builder.get_object("button_refresh").connect(
            "clicked", lambda _: partitioning.build_partitions(self))
        self.builder.get_object("treeview_disks").get_selection().connect(
            "changed", partitioning.update_html_preview)
        self.builder.get_object("treeview_disks").connect(
            "row_activated", partitioning.edit_partition_dialog)
        self.builder.get_object("treeview_disks").connect(
            "button-release-event", partitioning.partitions_popup_menu)
        text = Gtk.CellRendererText()
        for i in (partitioning.IDX_PART_PATH,
                  partitioning.IDX_PART_TYPE,
                  partitioning.IDX_PART_DESCRIPTION,
                  partitioning.IDX_PART_MOUNT_AS,
                  partitioning.IDX_PART_FORMAT_AS,
                  partitioning.IDX_PART_SIZE,
                  partitioning.IDX_PART_FREE_SPACE):
            # real title is set in i18n()
            col = Gtk.TreeViewColumn("", text, markup=i)
            self.builder.get_object("treeview_disks").append_column(col)

        self.builder.get_object("entry_name").connect(
            "notify::text", self.assign_realname)
        self.builder.get_object("entry_username").connect(
            "notify::text", self.assign_username)
        self.builder.get_object("entry_hostname").connect(
            "notify::text", self.assign_hostname)

        # events for detecting password mismatch..
        self.builder.get_object("entry_password").connect(
            "changed", self.assign_password)
        self.builder.get_object("entry_confirm").connect(
            "changed", self.assign_password)

        self.builder.get_object("radiobutton_passwordlogin").connect(
            "toggled", self.assign_login_options)

        # link the checkbutton to the combobox
        grub_check = self.builder.get_object("checkbutton_grub")
        grub_box = self.builder.get_object("combobox_grub")
        grub_check.connect("toggled", self.assign_grub_install, grub_box)
        grub_box.connect("changed", self.assign_grub_device)

        # install Grub by default
        grub_check.set_active(True)
        grub_box.set_sensitive(True)

        # kb models
        cell = Gtk.CellRendererText()
        cell.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
        self.builder.get_object("combobox_kb_model").pack_start(cell, True)
        self.builder.get_object(
            "combobox_kb_model").add_attribute(cell, 'text', 0)
        self.builder.get_object("combobox_kb_model").connect(
            "changed", self.assign_keyboard_model)

        # kb layouts
        ren = Gtk.CellRendererText()
        self.column10 = Gtk.TreeViewColumn(("Layout"), ren)
        self.column10.add_attribute(ren, "text", 0)
        self.builder.get_object(
            "treeview_layouts").append_column(self.column10)
        self.builder.get_object("treeview_layouts").connect(
            "cursor-changed", self.assign_keyboard_layout)

        ren = Gtk.CellRendererText()
        self.column11 = Gtk.TreeViewColumn(("Variant"), ren)
        self.column11.add_attribute(ren, "text", 0)
        self.builder.get_object(
            "treeview_variants").append_column(self.column11)
        self.builder.get_object("treeview_variants").connect(
            "cursor-changed", self.assign_keyboard_variant)

        self.build_kb_lists()

        # 'about to install' aka overview
        ren = Gtk.CellRendererText()
        self.column12 = Gtk.TreeViewColumn("", ren)
        self.column12.add_attribute(ren, "markup", 0)
        self.builder.get_object(
            "treeview_overview").append_column(self.column12)

        # install page
        self.builder.get_object("label_install_progress").set_text(
            ("Calculating file indexes ..."))
        self.builder.get_object("install_image").set_from_file(
            "./resources/install.png")
        

        # i18n
        self.i18n()

        # build partition list
        self.should_pulse = False

        # make sure we're on the right page (no pun.)
        self.activate_page(0)

        self.window.show_all()

    def fullscreen(self):
        self.window.fullscreen()
        self.window.set_titlebar(None)
        self.builder.get_object("vbox1").pack_start(self.builder.get_object("headerbar"),False,False,0)

    def i18n(self):

        window_title = ("Y??kleyiver")
        if os.path.isfile("/etc/lsb-release"):
            with open("/etc/lsb-release") as f:
                config = dict([line.strip().split("=") for line in f])
                window_title = "%s - %s" % (
                    config['DISTRIB_DESCRIPTION'].replace('"', ''), ("Y??kleyiver"))
        else:
           window_title = "Aylinux - %s" % ("Y??kleyiver")

        self.builder.get_object("button_expert").set_no_show_all(True)
        if self.expert_mode:
            window_title += ' (expert mode)'
            self.builder.get_object("button_expert").show()
        else:
            self.builder.get_object("button_expert").hide()
        self.window.set_title(window_title)

        # Header
        self.wizard_pages = list(range(12))
        self.wizard_pages[self.PAGE_WELCOME] = WizardPage(
            ("Ho??geldiniz"), "mark-location-symbolic", "")
        self.wizard_pages[self.PAGE_LANGUAGE] = WizardPage(
            ("Dil"), "preferences-desktop-locale-symbolic", ("Hangi dili kullanmak istersin?"))
        self.wizard_pages[self.PAGE_TIMEZONE] = WizardPage(
            ("Zaman dilimi"), "mark-location-symbolic", ("Neredesin?"))
        self.wizard_pages[self.PAGE_KEYBOARD] = WizardPage(
            ("Klavye d??zeni"), "preferences-desktop-keyboard-symbolic", ("Klavye d??zeniniz nedir?"))
        self.wizard_pages[self.PAGE_USER] = WizardPage(
            ("Kullan??c?? hesab??"), "avatar-default-symbolic", ("Siz kimsiniz?"))
        self.wizard_pages[self.PAGE_TYPE] = WizardPage(
            ("Kurulum t??r??"), "drive-harddisk-system-symbolic", ("Aylinux'u nereye kurmak istiyorsunuz?"))
        self.wizard_pages[self.PAGE_PARTITIONS] = WizardPage(
            ("B??l??m olu??turma"), "drive-harddisk-system-symbolic", ("Aylinux'u nereye kurmak istiyorsunuz?"))
        self.wizard_pages[self.PAGE_ADVANCED] = WizardPage(
            ("Geli??mi?? ayarlar"), "preferences-system-symbolic", "??ny??kleme men??s??n?? yap??land??r??n")
        self.wizard_pages[self.PAGE_OVERVIEW] = WizardPage(
            ("??zet"), "object-select-symbolic", "Her ??eyin do??ru olup olmad??????n?? kontrol edin")
        self.wizard_pages[self.PAGE_INSTALL] = WizardPage(
            ("Kuruluyor"), "system-run-symbolic", "L??tfen bekleyin...")
        self.wizard_pages[self.PAGE_CUSTOMWARNING] = WizardPage(
            ("Uzman modu"), "drive-harddisk-system-symbolic", "")
        self.wizard_pages[self.PAGE_CUSTOMPAUSED] = WizardPage(
            ("Kurulum durdu"), "system-run-symbolic", "")

        # Buttons
        self.builder.get_object("button_quit").set_label(("????k????"))
        self.builder.get_object("button_back").set_label(("Geri"))
        self.builder.get_object("button_next").set_label(("??leri"))

        # Welcome page
        self.builder.get_object("img_distro").set_from_file(
        "./resources/distro.png")
        self.builder.get_object("label_welcome1").set_text(
            ("Aylinux Y??kleyiciye ho?? geldiniz."))
        self.builder.get_object("label_welcome2").set_text(
            ("Bu program size baz?? sorular soracak ve Aylinux'u bilgisayar??n??za kuracak. "))

        # Language page
        self.language_column.set_title(("Dil"))
        self.country_column.set_title(("??lke"))

        # Keyboard page
        self.builder.get_object(
            "label_kb_model").set_label(("Klavye Modeli:"))
        self.column10.set_title(("D??zen"))
        self.column11.set_title(("Varyant"))
        self.builder.get_object("entry_test_kb").set_placeholder_text(
            ("Klavye d??zeninizi test etmek i??in buraya yaz??n"))
        self.builder.get_object("label_non_latin").set_text(
            ("* Kullan??c?? ad??n??z, bilgisayar??n??z??n ad?? ve ??ifreniz yaln??zca Latin karakterleri i??ermelidir. Se??ti??iniz d??zene ek olarak, ??ngilizce (ABD) varsay??lan olarak ayarlanm????t??r. Her iki Ctrl tu??una birlikte basarak d??zenler aras??nda ge??i?? yapabilirsiniz."))

        # User page
        self.builder.get_object("label_name").set_text(("Ad??n??z:"))
        self.builder.get_object("label_hostname").set_text(
            ("Bilgisayar??n??z??n ad??:"))
        self.builder.get_object("label_hostname_help").set_text(
            ("Di??er bilgisayarlarda g??r??necek ad."))
        self.builder.get_object("label_username").set_text(
            ("Bir kullan??c?? ad?? girin:"))
        self.builder.get_object("label_password").set_text(
            ("Bir ??ifre girin:"))
        self.builder.get_object("label_confirm").set_text(
            ("Parolan??z?? do??rulay??n:"))

        self.builder.get_object("radiobutton_autologin").set_label(
            ("Otomatik olarak oturum a????n"))
        self.builder.get_object("radiobutton_passwordlogin").set_label(
            ("Giri?? yapmak i??in ??ifreyi zorunlu yap"))

        # Type page
        self.builder.get_object("label_automated").set_text(
            ("Otomatik Kurulum"))
        self.builder.get_object("label_automated2").set_text(
            ("Bir diski silin ve ??zerine Aylinux'u kurun."))
        self.builder.get_object("label_disk").set_text(("Disk:"))
        self.builder.get_object("label_manual").set_text(
            ("Elle B??l??mleme"))
        self.builder.get_object("label_manual2").set_text(
            ("Aylinux i??in b??l??mleri manuel olarak olu??turun, yeniden boyutland??r??n veya se??in."))
        self.builder.get_object("label_badblocks").set_text(
            ("Diski rastgele verilerle doldurun"))
        self.builder.get_object("check_badblocks").set_tooltip_text(
            ("Bu ekstra g??venlik sa??lar ancak saatler s??rebilir."))

        # Partitions page
        self.builder.get_object("button_edit").set_label(("B??l??mleri d??zenle"))
        self.builder.get_object("button_refresh").set_label(("Yenile"))
        self.builder.get_object("button_expert").set_label(("Uzman Modu"))
        for col, title in zip(self.builder.get_object("treeview_disks").get_columns(),
                              (("Ayg??t"),
                               ("T??r"),
                               ("????letim Sistemi"),
                               ("Ba??lama Noktas??"),
                               ("Yeni Bi??im"),
                               ("Boyut"),
                               ("Kullan??labilir Alan"))):
            col.set_title(title)

        # Advanced page
        self.builder.get_object("checkbutton_grub").set_label(
            ("GRUB ??ny??kleme men??s??n?? ??uraya kurun:"))

        # Custom install warning
        self.builder.get_object("label_custom_install_directions_1").set_label(
            ("B??l??mlerinizi manuel olarak y??netmeyi se??tiniz, bu ??zellik YALNIZCA GEL????M???? KULLANICILAR i??indir."))
        self.builder.get_object("label_custom_install_directions_2").set_label(
            ("Devam etmeden ??nce, hedef dosya sistemlerinizi /target ??zerine ba??lay??n."))
        self.builder.get_object("label_custom_install_directions_3").set_label(
            ("Do NOT mount virtual devices such as /dev, /proc, /sys, etc on /target/."))
        self.builder.get_object("label_custom_install_directions_4").set_label(
            ("Kurulum s??ras??nda, /target i??ine chroot yap??p yeni sisteminizi ba??latmak i??in gerekli olacak paketleri kurman??z i??in zaman verilecektir."))
        self.builder.get_object("label_custom_install_directions_5").set_label(
            ("During the install, you will be required to write your own /etc/fstab."))

        # Custom install installation paused directions
        self.builder.get_object("label_custom_install_paused_1").set_label(
            ("A??a????dakileri yap??n ve ard??ndan kurulumu tamamlamak i??in ??leri'ye t??klay??n:"))
        self.builder.get_object("label_custom_install_paused_2").set_label(
            ("Create /target/etc/fstab for the filesystems as they will be mounted in your new system, matching those currently mounted at /target (without using the /target prefix in the mount paths themselves)."))
        self.builder.get_object("label_custom_install_paused_3").set_label(
            ("Install any packages that may be needed for first boot (mdadm, cryptsetup, dmraid, etc) by calling \"sudo chroot /target\" followed by the relevant apt-get/aptitude installations."))
        self.builder.get_object("label_custom_install_paused_4").set_label(
            ("Note that in order for update-initramfs to work properly in some cases (such as dm-crypt), you may need to have drives currently mounted using the same block device name as they appear in /target/etc/fstab."))
        self.builder.get_object("label_custom_install_paused_5").set_label(
            ("Double-check that your /target/etc/fstab is correct, matches what your new system will have at first boot, and matches what is currently mounted at /target."))

        # Refresh the current title and help question in the page header
        self.activate_page(self.PAGE_LANGUAGE)

    def assign_realname(self, entry, prop):
        self.setup.real_name = entry.props.text
        # Kullan??c?? ad??n?? ayarlamay?? deneyin (ba??ar??s??z olmas?? ??nemli de??il)
        try:
            text = entry.props.text.strip().lower()
            if " " in entry.props.text:
                elements = text.split()
                text = elements[0]
            self.setup.username = text
            self.builder.get_object("entry_username").set_text(text)
        except:
            pass
        if self.setup.real_name == "":
            self.builder.get_object("check_name").hide()
        else:
            self.builder.get_object("check_name").show()
        self.setup.print_setup()

    def assign_username(self, entry, prop):
        self.setup.username = entry.props.text
        errorFound = False
        for char in self.setup.username:
            if(char.isupper()):
                errorFound = True
                break
            elif(char.isspace()):
                errorFound = True
                break
        if errorFound or self.setup.username == "":
            self.builder.get_object("check_username").hide()
        else:
            self.builder.get_object("check_username").show()
        self.setup.print_setup()

    def assign_hostname(self, entry, prop):
        self.setup.hostname = entry.props.text
        errorFound = False
        for char in self.setup.hostname:
            if(char.isupper()):
                errorFound = True
                break
            elif(char.isspace()):
                errorFound = True
                break
        if errorFound or self.setup.hostname == "":
            self.builder.get_object("check_hostname").hide()
        else:
            self.builder.get_object("check_hostname").show()
        self.setup.print_setup()

    def assign_password(self, widget):
        self.setup.password1 = self.builder.get_object(
            "entry_password").get_text()
        self.setup.password2 = self.builder.get_object(
            "entry_confirm").get_text()

        if self.setup.password1 == "":
            self.builder.get_object("check_password").hide()
        else:
            self.builder.get_object("check_password").show()

        # Check the password confirmation
        if(self.setup.password1 == "" or self.setup.password2 == "" or self.setup.password1 != self.setup.password2):
            self.builder.get_object("check_confirm").hide()
        else:
            self.builder.get_object("check_confirm").show()

        self.setup.print_setup()

    def assign_type_options(self, widget, data=None):
        self.setup.automated = self.builder.get_object(
            "radio_automated").get_active()
        self.builder.get_object("check_badblocks").set_sensitive(
            self.setup.automated)
        self.builder.get_object("combo_disk").set_sensitive(
            self.setup.automated)
        if not self.setup.automated:
            self.builder.get_object("check_badblocks").set_active(False)
            self.builder.get_object("combo_disk").set_active(-1)


        model = self.builder.get_object("combo_disk").get_model()
        active = self.builder.get_object("combo_disk").get_active()
        if(active > -1):
            row = model[active]
            self.setup.disk = row[1]
            self.setup.diskname = row[0]
        self.builder.get_object("check_badblocks").set_sensitive(True)

        self.setup.badblocks = self.builder.get_object(
            "check_badblocks").get_active()

        self.setup.print_setup()

    def quit_cb(self, widget, data=None):
        if QuestionDialog(("Aylinux"), ("Y??kleyiciden ????kmak istedi??inizden emin misiniz?")):
            Gtk.main_quit()
            return False
        else:
            return True

    def show_customwarning(self, widget):
        self.activate_page(self.PAGE_CUSTOMWARNING)

    def build_lang_list(self):

        # Try to find out where we're located...
        try:
            from urllib.request import urlopen
        except ImportError:  # py3
            from urllib.request import urlopen
        try:
            lookup = str(urlopen('http://geoip.ubuntu.com/lookup').read())
            self.cur_country_code = re.search(
                '<CountryCode>(.*)</CountryCode>', lookup).group(1)
            self.cur_timezone = re.search(
                '<TimeZone>(.*)</TimeZone>', lookup).group(1)
            if self.cur_country_code == 'None':
                self.cur_country_code = "US"
            if self.cur_timezone == 'None':
                self.cur_timezone = "America/New_York"
        except:
            # no internet connection
            self.cur_country_code, self.cur_timezone = "US", "America/New_York"

        # Load countries into memory
        countries = {}
        iso_standard = "3166"
        if os.path.exists("/usr/share/xml/iso-codes/iso_3166-1.xml"):
            iso_standard = "3166-1"
        for line in subprocess.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
            ccode, cname = line.split(None, 1)
            countries[ccode] = cname

        # Load languages into memory
        languages = {}
        iso_standard = "639"
        if os.path.exists("/usr/share/xml/iso-codes/iso_639-2.xml"):
            iso_standard = "639-2"
        for line in subprocess.getoutput("isoquery --iso %s | cut -f3,4-" % iso_standard).split('\n'):
            cols = line.split(None, 1)
            if len(cols) > 1:
                name = cols[1].replace(";", ",")
                languages[cols[0]] = name
        for line in subprocess.getoutput("isoquery --iso %s | cut -f1,4-" % iso_standard).split('\n'):
            cols = line.split(None, 1)
            if len(cols) > 1:
                if cols[0] not in list(languages.keys()):
                    name = cols[1].replace(";", ",")
                    languages[cols[0]] = name

        # Construct language selection model
        model = Gtk.ListStore(str, str, GdkPixbuf.Pixbuf, str)
        set_iter = None
        def flag_path(ccode): return self.resource_dir + \
            '/flags/16/' + ccode.lower() + '.png'
        from utils import memoize
        language = None
        flag = memoize(
            lambda ccode: GdkPixbuf.Pixbuf.new_from_file(flag_path(ccode)))
        for locale in subprocess.getoutput("cat ./resources/locales").split('\n'):
            if '_' in locale:
                lang, ccode = locale.split('_')
                language = lang
                country = ccode
                try:
                    language = languages[lang]
                except:
                    pass
                try:
                    country = countries[ccode]
                except:
                    pass
            else:
                lang = locale
                try:
                    language = languages[lang]
                except:
                    pass
                country = ''
            pixbuf = flag(ccode) if not lang in 'eo ia' else flag('_' + lang)
            iter = model.append((language, country, pixbuf, locale))
            if (ccode == self.cur_country_code and
                (not set_iter or
                 set_iter and lang == 'en' or  # prefer English, or
                 set_iter and lang == ccode.lower())):  # fuzzy: lang matching ccode (fr_FR, de_DE, es_ES, ...)
                set_iter = iter

        # Sort by language then country
        model.set_sort_column_id(1, Gtk.SortType.ASCENDING)
        model.set_sort_column_id(0, Gtk.SortType.ASCENDING)
        # Set the model and pre-select the correct language
        treeview = self.builder.get_object("treeview_language_list")
        treeview.set_model(model)
        if set_iter:
            path = model.get_path(set_iter)
            treeview.set_cursor(path)
            treeview.scroll_to_cell(path)

    def build_kb_lists(self):
        ''' Do some xml kung-fu and load the keyboard stuffs '''
        # Determine the layouts in use
        (keyboard_geom,
         self.setup.keyboard_layout) = subprocess.getoutput("setxkbmap -query | awk '/^(model|layout)/{print $2}'").split()
        # Build the models
        from collections import defaultdict

        def _ListStore_factory():
            model = Gtk.ListStore(str, str)
            model.set_sort_column_id(0, Gtk.SortType.ASCENDING)
            return model
        models = _ListStore_factory()
        layouts = _ListStore_factory()
        variants = defaultdict(_ListStore_factory)
        try:
            import xml.etree.cElementTree as ET
        except ImportError:
            import xml.etree.ElementTree as ET
        xml = ET.parse('/usr/share/X11/xkb/rules/xorg.xml')
        for node in xml.iterfind('.//modelList/model/configItem'):
            name, desc = node.find('name').text, node.find('description').text
            iterator = models.append((desc, name))
            if name == keyboard_geom:
                set_keyboard_model = iterator
        for node in xml.iterfind('.//layoutList/layout'):
            name, desc = node.find(
                'configItem/name').text, node.find('configItem/description').text
            nonedesc = desc
            if name in NON_LATIN_KB_LAYOUTS:
                nonedesc = "English (US) + %s" % nonedesc
            variants[name].append((nonedesc, None))
            for variant in node.iterfind('variantList/variant/configItem'):
                var_name, var_desc = variant.find(
                    'name').text, variant.find('description').text
                var_desc = var_desc if var_desc.startswith(
                    desc) else '{} - {}'.format(desc, var_desc)
                if name in NON_LATIN_KB_LAYOUTS and "Latin" not in var_desc:
                    var_desc = "English (US) + %s" % var_desc
                variants[name].append((var_desc, var_name))
            if name in NON_LATIN_KB_LAYOUTS:
                desc = desc + " *"
            iterator = layouts.append((desc, name))
            if name == self.setup.keyboard_layout:
                set_keyboard_layout = iterator
        # Set the models
        self.builder.get_object("combobox_kb_model").set_model(models)
        self.builder.get_object("treeview_layouts").set_model(layouts)
        self.layout_variants = variants
        # Preselect currently active keyboard info
        try:
            self.builder.get_object(
                "combobox_kb_model").set_active_iter(set_keyboard_model)
        except NameError:
            pass  # set_keyboard_model not set
        try:
            treeview = self.builder.get_object("treeview_layouts")
            path = layouts.get_path(set_keyboard_layout)
            treeview.set_cursor(path)
            treeview.scroll_to_cell(path)
        except NameError:
            pass  # set_keyboard_layout not set

    def assign_language(self, treeview, data=None):
        ''' Called whenever someone updates the language '''
        model = treeview.get_model()
        selection = treeview.get_selection()
        (model, iter) = selection.get_selected()
        if iter is not None:
            self.setup.language = model.get_value(iter, 3)
            self.setup.print_setup()

    def assign_login_options(self, checkbox, data=None):
        self.setup.autologin = self.builder.get_object(
            "radiobutton_autologin").get_active()
        self.setup.print_setup()

    def assign_grub_install(self, checkbox, grub_box, data=None):
        grub_box.set_sensitive(checkbox.get_active())
        if checkbox.get_active():
            self.assign_grub_device(grub_box)
        else:
            self.setup.grub_device = None
        self.setup.print_setup()

    def assign_grub_device(self, combobox, data=None):
        ''' Called whenever someone updates the grub device '''
        model = combobox.get_model()
        active = combobox.get_active()
        if(active > -1):
            row = model[active]
            self.setup.grub_device = row[0]
        self.setup.print_setup()

    def assign_keyboard_model(self, combobox):
        ''' Called whenever someone updates the keyboard model '''
        model = combobox.get_model()
        active = combobox.get_active()
        (self.setup.keyboard_model_description,
         self.setup.keyboard_model) = model[active]
        os.system('setxkbmap -model ' + self.setup.keyboard_model)
        self.setup.print_setup()

    def assign_keyboard_layout(self, treeview):
        ''' Called whenever someone updates the keyboard layout '''
        model, active = treeview.get_selection().get_selected_rows()
        if not active:
            return
        (self.setup.keyboard_layout_description,
         self.setup.keyboard_layout) = model[active[0]]
        # Set the correct variant list model ...
        model = self.layout_variants[self.setup.keyboard_layout]
        self.builder.get_object("treeview_variants").set_model(model)
        # ... and select the first variant (standard)
        self.builder.get_object("treeview_variants").set_cursor(0)

    def assign_keyboard_variant(self, treeview):
        ''' Called whenever someone updates the keyboard layout or variant '''
        # GObject.source_remove(self.kbd_preview_generation)  # stop previous preview generation, if any
        model, active = treeview.get_selection().get_selected_rows()
        if not active:
            return
        (self.setup.keyboard_variant_description,
         self.setup.keyboard_variant) = model[active[0]]

        if self.setup.keyboard_variant is None:
            self.setup.keyboard_variant = ""

        if self.setup.keyboard_layout in NON_LATIN_KB_LAYOUTS:
            # Add US layout for non-latin layouts
            self.setup.keyboard_layout = 'us,%s' % self.setup.keyboard_layout

        if "Latin" in self.setup.keyboard_variant_description:
            # Remove US layout for Latin variants
            self.setup.keyboard_layout = self.setup.keyboard_layout.replace(
                "us,", "")

        if "us," in self.setup.keyboard_layout:
            # Add None variant for US layout
            self.setup.keyboard_variant = ',%s' % self.setup.keyboard_variant

        if "us," in self.setup.keyboard_layout:
            self.builder.get_object("label_non_latin").show()
        else:
            self.builder.get_object("label_non_latin").hide()

        command = "setxkbmap -layout '%s' -variant '%s' -option grp:ctrls_toggle" % (
            self.setup.keyboard_layout, self.setup.keyboard_variant)
        os.system(command)
        self.setup.print_setup()

        # Remove preview image
        self.builder.get_object("image_keyboard").hide()
        self.builder.get_object("kb_spinner").hide()

    @idle
    def _on_layout_generated(self):
        filename = "/tmp/live-install-keyboard-layout.png"

        self.builder.get_object("kb_spinner").stop()
        self.builder.get_object("kb_spinner").hide()

        widget = self.builder.get_object("image_keyboard")
        widget.show()

        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(filename)
            surface = Gdk.cairo_surface_create_from_pixbuf(
                pixbuf, widget.get_scale_factor(), widget.get_window())
            widget.set_from_surface(surface)
        except GLib.Error as e:
            print(("could not load keyboard layout: %s" % e.message))
        return False

    def activate_page(self, index):
        # progress images
        for i in range(9):
            img = self.builder.get_object("progress_%d" % i)
            if i <= index:
                img.set_from_file(
                    "./icons/live-installer-progress-dot-on.png")
            else:
                img.set_from_file(
                    "./icons/live-installer-progress-dot-off.png")
        help_text = (self.wizard_pages[index].help_text)
        self.builder.get_object("help_label").set_markup(
            "<big><b>%s</b></big>" % help_text)
        self.builder.get_object("help_icon").set_from_icon_name(
            self.wizard_pages[index].icon, Gtk.IconSize.LARGE_TOOLBAR)
        self.builder.get_object("help_question").set_text(
            self.wizard_pages[index].question)
        self.builder.get_object("notebook1").set_current_page(index)
        # TODO: move other page-depended actions from the wizard_cb into here below
        if index == self.PAGE_PARTITIONS:
            self.setup.skip_mount = False
        if index == self.PAGE_CUSTOMWARNING:
            self.setup.skip_mount = True

    def wizard_cb(self, widget, goback, data=None):
        ''' wizard buttons '''
        sel = self.builder.get_object("notebook1").get_current_page()
        self.builder.get_object("button_back").set_sensitive(True)

        # check each page for errors
        if(not goback):
            if (sel == self.PAGE_WELCOME):
                self.activate_page(self.PAGE_LANGUAGE)
            elif(sel == self.PAGE_LANGUAGE):
                if self.setup.language is None:
                    WarningDialog(("Aylinux-Y??kleyiver"), ("L??tfen bir dil se??in"))
                else:
                    lang_country_code = self.setup.language.split('_')[-1]
                    for value in (self.cur_timezone,      # timezone guessed from IP
                                  self.cur_country_code,  # otherwise pick country from IP
                                  lang_country_code):     # otherwise use country from language selection
                        if not value:
                            continue
                        for row in timezones.timezones:
                            if value in row:
                                timezones.select_timezone(row)
                                break
                        break
                    self.activate_page(self.PAGE_TIMEZONE)
            elif (sel == self.PAGE_TIMEZONE):
                if ("_" in self.setup.language):
                    country_code = self.setup.language.split("_")[1]
                else:
                    country_code = self.setup.language
                treeview = self.builder.get_object("treeview_layouts")
                model = treeview.get_model()
                iter = model.get_iter_first()
                while iter is not None:
                    iter_country_code = model.get_value(iter, 1)
                    if iter_country_code.lower() == country_code.lower():
                        column = treeview.get_column(0)
                        path = model.get_path(iter)
                        treeview.set_cursor(path)
                        treeview.scroll_to_cell(path, column=column)
                        break
                    iter = model.iter_next(iter)
                self.activate_page(self.PAGE_KEYBOARD)
            elif(sel == self.PAGE_KEYBOARD):
                self.activate_page(self.PAGE_USER)
                self.builder.get_object("entry_name").grab_focus()
            elif(sel == self.PAGE_USER):
                errorFound = False
                errorMessage = ""
                focus_widget = None

                if(self.setup.real_name is None or self.setup.real_name == ""):
                    errorFound = True
                    errorMessage = ("L??tfen tam ad??n??z?? girin.")
                    focus_widget = self.builder.get_object("entry_name")
                elif(self.setup.hostname is None or self.setup.hostname == ""):
                    errorFound = True
                    errorMessage = ("L??tfen bilgisayar??n??z i??in bir isim girin.")
                    focus_widget = self.builder.get_object("entry_hostname")
                elif(self.setup.username is None or self.setup.username == ""):
                    errorFound = True
                    errorMessage = ("L??tfen bir kullan??c?? ad?? girin.")
                    focus_widget = self.builder.get_object("entry_username")
                elif(self.setup.password1 is None or self.setup.password1 == ""):
                    errorFound = True
                    errorMessage = ("L??tfen kullan??c?? hesab??n??z i??in bir ??ifre girin.")
                    focus_widget = self.builder.get_object("entry_password")
                elif(self.setup.password1 != self.setup.password2):
                    errorFound = True
                    errorMessage = ("??ifreleriniz birbirine uymuyor.")
                    focus_widget = self.builder.get_object("entry_confirm")
                else:
                    for char in self.setup.username:
                        if(char.isupper()):
                            errorFound = True
                            errorMessage = (
                                "Kullan??c?? ad??n??z k??????k harf olmal??d??r.")
                            focus_widget = self.builder.get_object(
                                "entry_username")
                            break
                        elif(char.isspace()):
                            errorFound = True
                            errorMessage = (
                                "Kullan??c?? ad??n??z bo??luk karakterleri i??eremez.")
                            focus_widget = self.builder.get_object(
                                "entry_username")
                            break
                    for char in self.setup.hostname:
                        if(char.isupper()):
                            errorFound = True
                            errorMessage = (
                                "Bilgisayar??n ad?? k??????k harf olmal??d??r.")
                            focus_widget = self.builder.get_object(
                                "entry_hostname")
                            break
                        elif(char.isspace()):
                            errorFound = True
                            errorMessage = (
                                "Bilgisayar??n ad?? bo??luk karakterleri i??eremez.")
                            focus_widget = self.builder.get_object(
                                "entry_hostname")
                            break

                if (errorFound):
                    WarningDialog(("Aylinux-Y??kleyiver"), errorMessage)
                    if focus_widget is not None:
                        focus_widget.grab_focus()
                else:
                    self.activate_page(self.PAGE_TYPE)
            elif(sel == self.PAGE_TYPE):
                if self.setup.automated:
                    errorFound = False
                    errorMessage = ""
                    if self.setup.disk is None:
                        errorFound = True
                        errorMessage = ("L??tfen bir disk se??in.")
                    if (errorFound):
                        WarningDialog(("Aylinux-Y??kleyiver"), errorMessage)
                    else:
                        if QuestionDialog(("Warning"), ("Bu i??lem %s ??zerindeki t??m verileri silecek. Emin misiniz?") % self.setup.diskname):
                            partitioning.build_partitions(self)
                            partitioning.build_grub_partitions()
                            self.activate_page(self.PAGE_ADVANCED)
                else:
                    self.activate_page(self.PAGE_PARTITIONS)
                    partitioning.build_partitions(self)
            elif(sel == self.PAGE_PARTITIONS):
                model = self.builder.get_object("treeview_disks").get_model()

                # Check for root partition
                found_root_partition = False
                for partition in self.setup.partitions:
                    if(partition.mount_as == "/"):
                        found_root_partition = True
                        if partition.format_as is None or partition.format_as == "":
                            ErrorDialog(
                                ("Aylinux-Y??kleyiver"), ("Please indicate a filesystem to format the root (/) partition with before proceeding."))
                            return
                    if partition.mount_as == "/@":
                        if partition.format_as != "btrfs":
                            ErrorDialog(
                                ("Aylinux-Y??kleyiver"), ("A root subvolume (/@) requires to format the partition with btrfs."))
                            return
                        found_root_partition = True
                    if partition.mount_as == "/@home":
                        if partition.format_as == "btrfs":
                            continue
                        if partition.type == "btrfs" and (partition.format_as == None or partition.format_as == ""):
                            continue
                        ErrorDialog(
                            ("Aylinux-Y??kleyiver"), ("A home subvolume (/@home) requires the use of a btrfs formatted partition."))
                        return

                if not found_root_partition:
                    ErrorDialog(("Aylinux-Y??kleyiver"), "<b>%s</b>" % ("Please select a root (/) partition."), (
                        "A root partition is needed to install Linux Mint on.\n\n"
                        " - Mount point: /\n - Recommended size: 30GB\n"
                        " - Recommended filesystem format: ext4\n\n"
                        "Note: The timeshift btrfs snapshots feature requires the use of:\n"
                        " - subvolume Mount-point /@\n"
                        " - btrfs as filesystem format\n"))
                    return

                if self.setup.gptonefi:
                    # Check for an EFI partition
                    found_efi_partition = False
                    for partition in self.setup.partitions:
                        if(partition.mount_as == "/boot/efi"):
                            found_efi_partition = True
                            if not partition.partition.getFlag(parted.PARTITION_BOOT):
                                ErrorDialog(
                                    ("Aylinux-Y??kleyiver"), ("EFI b??l??m?? ??ny??klenebilir de??ildir. L??tfen b??l??m bayraklar??n?? d??zenleyin."))
                                return
                            if int(float(partition.partition.getLength('MB'))) < 35:
                                ErrorDialog(
                                    ("Aylinux-Y??kleyiver"), ("EFI b??l??m?? ??ok k??????k. En az 35MB olmal??d??r."))
                                return
                            if partition.format_as == None or partition.format_as == "":
                                # No partitioning
                                if partition.type != "vfat" and partition.type != "fat32" and partition.type != "fat16":
                                    ErrorDialog(
                                        ("Aylinux-Y??kleyiver"), ("EFI b??l??m?? vfat olarak bi??imlendirilmelidir."))
                                    return
                            else:
                                if partition.format_as != "vfat":
                                    ErrorDialog(
                                        ("Aylinux-Y??kleyiver"), ("EFI b??l??m?? vfat olarak bi??imlendirilmelidir."))
                                    return

                    if not found_efi_partition:
                        ErrorDialog(("Aylinux-Y??kleyiver"), "<b>%s</b>" % ("Please select an EFI partition."),
                                    ("An EFI system partition is needed with the following requirements:\n\n - Mount point: /boot/efi\n - Partition flags: Bootable\n - Size: at least 35MB (100MB or more recommended)\n - Format: vfat or fat32\n\nTo ensure compatibility with Windows we recommend you use the first partition of the disk as the EFI system partition.\n "))
                        return

                partitioning.build_grub_partitions()
                self.activate_page(self.PAGE_ADVANCED)

            elif(sel == self.PAGE_CUSTOMWARNING):
                partitioning.build_grub_partitions()
                self.activate_page(self.PAGE_ADVANCED)
            elif(sel == self.PAGE_ADVANCED):
                self.activate_page(self.PAGE_OVERVIEW)
                self.show_overview()
                self.builder.get_object("treeview_overview").expand_all()
                self.builder.get_object("button_next").set_label(("Kur"))
            elif(sel == self.PAGE_OVERVIEW):
                self.activate_page(self.PAGE_INSTALL)
                self.builder.get_object("button_next").set_sensitive(False)
                self.builder.get_object("button_back").set_sensitive(False)
                self.builder.get_object("button_quit").set_sensitive(False)
                self.do_install()
                self.builder.get_object("title_eventbox").hide()
                self.builder.get_object("button_eventbox").hide()
                #self.window.resize(100, 100)
            elif(sel == self.PAGE_CUSTOMPAUSED):
                self.activate_page(self.PAGE_INSTALL)
                self.builder.get_object("button_next").set_sensitive(False)
                self.builder.get_object("button_back").set_sensitive(False)
                self.builder.get_object("button_quit").set_sensitive(False)
                self.builder.get_object("title_eventbox").hide()
                self.builder.get_object("button_eventbox").hide()
                self.window.resize(100, 100)
                self.paused = False
        else:
            self.builder.get_object("button_back").set_sensitive(True)
            if(sel == self.PAGE_OVERVIEW):
                self.activate_page(self.PAGE_ADVANCED)
            elif(sel == self.PAGE_ADVANCED):
                if (self.setup.skip_mount):
                    self.activate_page(self.PAGE_CUSTOMWARNING)
                elif self.setup.automated:
                    self.activate_page(self.PAGE_TYPE)
                else:
                    self.activate_page(self.PAGE_PARTITIONS)
            elif(sel == self.PAGE_CUSTOMWARNING):
                self.activate_page(self.PAGE_PARTITIONS)
            elif(sel == self.PAGE_PARTITIONS):
                self.activate_page(self.PAGE_TYPE)
            elif(sel == self.PAGE_TYPE):
                self.activate_page(self.PAGE_USER)
            elif(sel == self.PAGE_USER):
                self.activate_page(self.PAGE_KEYBOARD)
            elif(sel == self.PAGE_KEYBOARD):
                self.activate_page(self.PAGE_TIMEZONE)
            elif(sel == self.PAGE_TIMEZONE):
                self.activate_page(self.PAGE_LANGUAGE)
            elif(sel == self.PAGE_LANGUAGE):
                self.activate_page(self.PAGE_WELCOME)

    def show_overview(self):
        def bold(str): return '<b>' + str + '</b>'
        model = Gtk.TreeStore(str)
        self.builder.get_object("treeview_overview").set_model(model)
        top = model.append(None, (("Localization"),))
        model.append(top, (("Language: ") + bold(self.setup.language),))
        model.append(top, (("Timezone: ") + bold(self.setup.timezone),))
        model.append(top, (("Keyboard layout: ") +
                           "<b>%s - %s %s</b>" % (self.setup.keyboard_model_description, self.setup.keyboard_layout_description,
                                                  '(%s)' % self.setup.keyboard_variant_description if self.setup.keyboard_variant_description else ''),))
        top = model.append(None, (("Kullan??c?? ayarlar??"),))
        model.append(top, (("Ger??ek isim: ") + bold(self.setup.real_name),))
        model.append(top, (("Kullan??c?? ad??: ") + bold(self.setup.username),))
        model.append(top, (("Otomatik giri??: ") + bold(("enabled")
                                                        if self.setup.autologin else ("disabled")),))
        top = model.append(None, (("Sistem ayarlar??"),))
        model.append(top, (("Bilgisayar ad??: ") + bold(self.setup.hostname),))
        top = model.append(None, (("Filesystem operations"),))
        model.append(top, (bold(("Install bootloader on %s") % self.setup.grub_device)
                           if self.setup.grub_device else ("Bootloader y??klemeyin"),))
        if self.setup.skip_mount:
            model.append(top, (bold(("Use already-mounted /target.")),))
            return
        if self.setup.automated:
            model.append(
                top, (bold(("Automated installation on %s") % self.setup.diskname),))
        else:
            for p in self.setup.partitions:
                if p.format_as:
                    model.append(top, (bold(("Format %(path)s as %(filesystem)s") % {
                                 'path': p.path, 'filesystem': p.format_as}),))
            for p in self.setup.partitions:
                if p.mount_as:
                    model.append(top, (bold(("Mount %(path)s as %(mount)s") % {
                                 'path': p.path, 'mount': p.mount_as}),))

    @idle
    def show_error_dialog(self, message, detail):
        ErrorDialog(message, detail)
        if self.showing_last_dialog:
            self.showing_last_dialog = False

    @idle
    def show_reboot_dialog(self):
        reboot = QuestionDialog(("Kurulum tamamland??"), (
            "Kurulum tamamland??. Yeni sistemi kullanmak i??in bilgisayar??n??z?? yeniden ba??latmak istiyor musunuz?"))
        if self.showing_last_dialog:
            self.showing_last_dialog = False
        if reboot:
            os.system('reboot')

    @idle
    def pause_installation(self):
        self.activate_page(self.PAGE_CUSTOMPAUSED)
        self.builder.get_object("button_next").set_sensitive(True)
        self.builder.get_object("button_next").set_label(("??leri"))
        self.builder.get_object("button_back").hide()
        self.builder.get_object("button_quit").hide()
        self.builder.get_object("title_eventbox").show()
        self.builder.get_object("button_eventbox").show()
        MessageDialog(("Kurulum duraklat??ld??"), (
            "Kurulum ??imdi duraklat??lm????t??r. Kurulumu bitirmek i??in ??leri'ye t??klamadan ??nce l??tfen sayfadaki talimatlar?? dikkatlice okuyun."))

    @asynchronous
    def do_install(self):
        print(" ## INSTALLATION ")
        ''' Actually perform the installation .. '''

        self.installer.set_progress_hook(self.update_progress)
        self.installer.set_error_hook(self.error_message)

        # do we dare? ..
        self.critical_error_happened = False

        # Start installing
        do_try_finish_install = True

        try:
            self.installer.start_installation()
        except Exception as detail1:
            print(detail1)
            do_try_finish_install = False
            self.show_error_dialog(("Installation error"), str(detail1))

        if self.critical_error_happened:
            self.show_error_dialog(
                ("Installation error"), self.critical_error_message)
            do_try_finish_install = False

        if do_try_finish_install:
            if(self.setup.skip_mount):
                self.paused = True
                self.pause_installation()
                while(self.paused):
                    time.sleep(0.1)

            try:
                self.installer.finish_installation()
            except Exception as detail1:
                print(detail1)
                self.show_error_dialog(("Installation error"), str(detail1))

            # show a message dialog thingum
            while(not self.done):
                time.sleep(0.1)

            self.showing_last_dialog = True
            if self.critical_error_happened:
                self.show_error_dialog(
                    ("Installation error"), self.critical_error_message)
            else:
                self.show_reboot_dialog()

            while(self.showing_last_dialog):
                time.sleep(0.1)

            print(" ## INSTALLATION COMPLETE ")

        Gtk.main_quit()
        sys.exit(0)

    def error_message(self, message=""):
        self.critical_error_happened = True
        self.critical_error_message = message

    @idle
    def update_progress(self, current, total, pulse, done, message):
        if(pulse):
            self.builder.get_object(
                "label_install_progress").set_label(message)
            self.do_progress_pulse(message)
            return
        if(done):
            self.should_pulse = False
            self.done = done
            self.builder.get_object("progressbar").set_fraction(1)
            self.builder.get_object(
                "label_install_progress").set_label(message)
            return
        self.should_pulse = False
        _total = float(total)
        _current = float(current)
        pct = float(_current/_total)
        szPct = int(pct)
        self.builder.get_object("progressbar").set_fraction(pct)
        self.builder.get_object("label_install_progress").set_label(message)

    @idle
    def do_progress_pulse(self, message):
        def pbar_pulse():
            if(not self.should_pulse):
                return False
            self.builder.get_object("progressbar").pulse()
            return self.should_pulse
        if(not self.should_pulse):
            self.should_pulse = True
            GObject.timeout_add(100, pbar_pulse)
        else:
            # asssume we're "pulsing" already
            self.should_pulse = True
            pbar_pulse()

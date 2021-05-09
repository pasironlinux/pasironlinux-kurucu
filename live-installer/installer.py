import os
import re
import subprocess
import time
import shutil
import subprocess
import sys
import parted
import partitioning


NON_LATIN_KB_LAYOUTS = ['am', 'af', 'ara', 'ben', 'bd', 'bg', 'bn', 'bt', 'by', 'deva', 'et', 'ge', 'gh', 'gn', 'gr', 'guj', 'guru', 'id', 'il', 'iku', 'in', 'iq', 'ir', 'kan',
                        'kg', 'kh', 'kz', 'la', 'lao', 'lk', 'ma', 'mk', 'mm', 'mn', 'mv', 'mal', 'my', 'np', 'ori', 'pk', 'ru', 'rs', 'scc', 'sy', 'syr', 'tel', 'th', 'tj', 'tam', 'tz', 'ua', 'uz']


class InstallerEngine:
    ''' This is central to the live installer '''

    def __init__(self, setup):
        self.setup = setup

        # Flush print when it's called
        #sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

        # find the squashfs..
        self.media = '/dev/loop0'
        if(not os.path.exists(self.media)):
            print("Önemli Hata: Canlı ortam (%s) bulunamadı!" % self.media)
            # sys.exit(1)

    def set_progress_hook(self, progresshook):
        ''' Set a callback to be called on progress updates '''
        ''' i.e. def my_callback(progress_type, message, current_progress, total) '''
        ''' Where progress_type is any off PROGRESS_START, PROGRESS_UPDATE, PROGRESS_COMPLETE, PROGRESS_ERROR '''
        self.update_progress = progresshook

    def set_error_hook(self, errorhook):
        ''' Set a callback to be called on errors '''
        self.error_message = errorhook

    def start_installation(self):

        # mount the media location.
        print(" --> Installation started")
        if(not os.path.exists("/target")):
            if (self.setup.skip_mount):
                self.error_message(message=(
                    "HATA: Özel bir kurulum yapmak için önce hedef dosya sistemlerinizi / target konumuna manuel olarak bağlamalısınız!"))
                return
            os.mkdir("/target")
        if(not os.path.exists("/source")):
            os.mkdir("/source")

        os.system("umount --force /target/dev/shm")
        os.system("umount --force /target/dev/pts")
        os.system("umount --force /target/dev/")
        os.system("umount --force /target/sys/")
        os.system("umount --force /target/proc/")
        os.system("umount --force /target/run/")

        self.mount_source()
        

        if (not self.setup.skip_mount):
            if self.setup.automated:
                self.create_partitions()
            else:
                self.format_partitions()
                self.mount_partitions()

        self.run_preinstall()
        
        # Transfer the files
        SOURCE = "/source/"
        DEST = "/target/"
        EXCLUDE_DIRS = "data/* dev/* proc/* sys/* tmp/* run/* lost+found source target".split()
        our_current = 0
        # (Valid) assumption: num-of-files-to-copy ~= num-of-used-inodes-on-/
        our_total = int(subprocess.getoutput(
            "df --inodes /{src} | awk 'END{{ print $3 }}'".format(src=SOURCE.strip('/'))))
        print(" --> {} dosyaları kopyalanıyor".format(our_total))
        rsync_filter = ' '.join(
            '--exclude=' + SOURCE + d for d in EXCLUDE_DIRS)
        rsync = subprocess.Popen("rsync --verbose --archive --no-D --acls "
                                 "--hard-links --xattrs {rsync_filter} "
                                 "{src}* {dst}".format(src=SOURCE,
                                                       dst=DEST, rsync_filter=rsync_filter),
                                 shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        while rsync.poll() is None:
            line = str(rsync.stdout.readline())
            line = line.replace("b'", "'")
            line = line.replace("'", "")
            line = line.replace("\\n", "")
            if not line:  # still copying the previous file, just wait
                time.sleep(0.1)
            else:
                our_current = min(our_current + 1, our_total)
                self.update_progress(our_current, our_total,
                                     False, False, ("Kopyalanıyor /%s") % line)
        print("rsync exited with returncode: " + str(rsync.poll()))

        # Steps:
        our_total = 11
        our_current = 0
        # chroot
        print(" --> Chrooting")
        self.update_progress(our_current, our_total, False,
                             False, ("Entering the system ..."))
        os.system("mount --bind /dev/ /target/dev/")
        os.system("mount --bind /dev/shm /target/dev/shm")
        os.system("mount --bind /dev/pts /target/dev/pts")
        os.system("mount --bind /sys/ /target/sys/")
        os.system("mount --bind /proc/ /target/proc/")
        os.system("mount --bind /run/ /target/run/")
        os.system("mv /target/etc/resolv.conf /target/etc/resolv.conf.bk")
        os.system("cp -f /etc/resolv.conf /target/etc/resolv.conf")

        self.run_postinstall()
        
        # add new user
        print(" --> Yeni kullanıcı ekleniyor")
        our_current += 1
        self.update_progress(our_current, our_total, False,
                             False, ("Yeni kullanıcı sisteme ekleniyor"))
        #TODO: support encryption
        self.do_run_in_chroot('useradd {username}'.format(username=self.setup.username))
        self.do_run_in_chroot("echo -ne \"{0}\\n{0}\\n\" | passwd {1}".format(self.setup.password1,self.setup.username))
        self.do_run_in_chroot("echo -ne \"{0}\\n{0}\\n\" | passwd".format(self.setup.password1))
        for g in ['audio', 'video', 'wheel']:
            self.do_run_in_chroot('usermod -a -G {group} {username}'.format(group=g, username=self.setup.username))
        #Create Userspace area
        self.do_run_in_chroot('mkdir -p /data/app/{username}'.format(username=self.setup.username))
        self.do_run_in_chroot('chmod -R 641 /data/app/{username}'.format(username=self.setup.username))
        self.do_run_in_chroot('chown -R {username} /data/app/{username}'.format(username=self.setup.username))

        # Set autologin for user if they so elected
        if self.setup.autologin:
            # LightDM
            self.do_run_in_chroot(
                r"sed -i -r 's/^#?(autologin-user)\s*=.*/\1={user}/' /etc/lightdm/lightdm.conf".format(user=self.setup.username))

        # /etc/fstab, mtab and crypttab
        our_current += 1
        self.update_progress(our_current, our_total, False, False,
                             ("Writing filesystem mount information to /etc/fstab"))
        self.write_fstab()

    def mount_source(self):
        # Mount the installation media
        print(" --> Mounting partitions")
        self.update_progress(2, 4, False, False, ("Mounting %(partition)s on %(mountpoint)s") % {
                             'partition': self.media, 'mountpoint': "/source/"})
        print(" ------ Mounting %s on %s" % (self.media, "/source/"))
        self.do_mount(self.media, "/source/", "squashfs", options="loop")

    def create_partitions(self):
        # Create partitions on the selected disk (automated installation)
        partition_prefix = ""
        if self.setup.disk.startswith("/dev/nvme"):
            partition_prefix = "p"
        if self.setup.luks:
            if self.setup.gptonefi:
                # EFI+LUKS/LVM
                # sdx1=EFI, sdx2=BOOT, sdx3=ROOT
                self.auto_efi_partition = self.setup.disk + partition_prefix + "1"
                self.auto_boot_partition = self.setup.disk + partition_prefix + "2"
                self.auto_swap_partition = None
                self.auto_root_partition = self.setup.disk + partition_prefix + "3"
            else:
                # BIOS+LUKS/LVM
                # sdx1=BOOT, sdx2=ROOT
                self.auto_efi_partition = None
                self.auto_boot_partition = self.setup.disk + partition_prefix + "1"
                self.auto_swap_partition = None
                self.auto_root_partition = self.setup.disk + partition_prefix + "2"
        elif self.setup.lvm:
            if self.setup.gptonefi:
                # EFI+LVM
                # sdx1=EFI, sdx2=ROOT
                self.auto_efi_partition = self.setup.disk + partition_prefix + "1"
                self.auto_boot_partition = None
                self.auto_swap_partition = None
                self.auto_root_partition = self.setup.disk + partition_prefix + "2"
            else:
                # BIOS+LVM:
                # sdx1=ROOT
                self.auto_efi_partition = None
                self.auto_boot_partition = None
                self.auto_swap_partition = None
                self.auto_root_partition = self.setup.disk + partition_prefix + "1"
        else:
            if self.setup.gptonefi:
                # EFI
                # sdx1=EFI, sdx2=SWAP, sdx3=ROOT
                self.auto_efi_partition = self.setup.disk + partition_prefix + "1"
                self.auto_boot_partition = None
                self.auto_swap_partition = self.setup.disk + partition_prefix + "2"
                self.auto_root_partition = self.setup.disk + partition_prefix + "3"
            else:
                # BIOS:
                # sdx1=SWAP, sdx2=ROOT
                self.auto_efi_partition = None
                self.auto_boot_partition = None
                self.auto_swap_partition = self.setup.disk + partition_prefix + "1"
                self.auto_root_partition = self.setup.disk + partition_prefix + "2"

        self.auto_root_physical_partition = self.auto_root_partition

        # Wipe HDD
        if self.setup.badblocks:
            self.update_progress(1, 4, False, False, (
                "Filling %s with random data (please be patient, this can take hours...)") % self.setup.disk)
            print(" --> Filling %s with random data" % self.setup.disk)
            os.system("badblocks -c 10240 -s -w -t random -v %s" %
                      self.setup.disk)

        # Create partitions
        self.update_progress(1, 4, False, False,
                             ("%s üzerinde bölümler oluşturuluyor") % self.setup.disk)
        print(" --> Creating partitions on %s" % self.setup.disk)
        disk_device = parted.getDevice(self.setup.disk)
        partitioning.full_disk_format(disk_device, create_boot=(
            self.auto_boot_partition is not None), create_swap=(self.auto_swap_partition is not None))

        self.do_mount(self.auto_root_partition, "/target", "ext4", None)
        if (self.auto_boot_partition is not None):
            os.system("mkdir -p /target/boot")
            self.do_mount(self.auto_boot_partition,
                          "/target/boot", "ext4", None)
        if (self.auto_efi_partition is not None):
            os.system("mkdir -p /target/boot/efi")
            self.do_mount(self.auto_efi_partition,
                          "/target/boot/efi", "vfat", None)

    def format_partitions(self):
        for partition in self.setup.partitions:
            if(partition.format_as is not None and partition.format_as != ""):
                # report it. should grab the total count of filesystems to be formatted ..
                self.update_progress(1, 4, True, False, ("Formatting %(partition)s as %(format)s ...") % {
                                     'partition': partition.path, 'format': partition.format_as})

                # Format it
                if partition.format_as == "swap":
                    cmd = "mkswap %s" % partition.path
                else:
                    if (partition.format_as in ['ext2', 'ext3', 'ext4']):
                        cmd = "mkfs.%s -F %s" % (partition.format_as,
                                                 partition.path)
                    elif (partition.format_as == "jfs"):
                        cmd = "mkfs.%s -q %s" % (partition.format_as,
                                                 partition.path)
                    elif (partition.format_as in ["btrfs", "xfs"]):
                        cmd = "mkfs.%s -f %s" % (partition.format_as,
                                                 partition.path)
                    elif (partition.format_as == "vfat"):
                        cmd = "mkfs.%s %s -F 32" % (partition.format_as,
                                                    partition.path)
                    else:
                        # works with bfs, minix, msdos, ntfs, vfat
                        cmd = "mkfs.%s %s" % (
                            partition.format_as, partition.path)

                print("EXECUTING: '%s'" % cmd)
                self.exec_cmd(cmd)
                partition.type = partition.format_as

    def mount_partitions(self):
        # Mount the target partition
        for partition in self.setup.partitions:
            if(partition.mount_as is not None and partition.mount_as != ""):
                if partition.mount_as == "/":
                    self.update_progress(3, 4, False, False, ("Mounting %(partition)s on %(mountpoint)s") % {
                                         'partition': partition.path, 'mountpoint': "/target/"})
                    print(" ------ Mounting partition %s on %s" %
                          (partition.path, "/target/"))
                    if partition.type == "fat32":
                        fs = "vfat"
                    else:
                        fs = partition.type
                    self.do_mount(partition.path, "/target", fs, None)
                    break

                if partition.mount_as == "/@":
                    if partition.type != "btrfs":
                        self.error_message(
                            message=("ERROR: the use of @subvolumes is limited to btrfs"))
                        return
                    print("btrfs using /@ subvolume...")
                    self.update_progress(3, 4, False, False, ("Mounting %(partition)s on %(mountpoint)s") % {
                                         'partition': partition.path, 'mountpoint': "/target/"})
                    # partition.mount_as = "/"
                    print(" ------ Mounting partition %s on %s" %
                          (partition.path, "/target/"))
                    fs = partition.type
                    self.do_mount(partition.path, "/target", fs, None)
                    os.system("btrfs subvolume create /target/@")
                    os.system("btrfs subvolume list -p /target")
                    print(" ------ Umount btrfs to remount subvolume /@")
                    os.system("umount --force /target")
                    self.do_mount(partition.path, "/target", fs, "subvol=@")
                    break

        # handle btrfs /@home subvolume-option after mounting / or /@
        for partition in self.setup.partitions:
            if(partition.mount_as is not None and partition.mount_as != ""):
                if partition.mount_as == "/@home":
                    if partition.type != "btrfs":
                        self.error_message(
                            message=("ERROR: the use of @subvolumes is limited to btrfs"))
                        return
                    print("btrfs using /@home subvolume...")
                    self.update_progress(3, 4, False, False, ("Mounting %(partition)s on %(mountpoint)s") % {
                                         'partition': partition.path, 'mountpoint': "/target/"})
                    print(" ------ Mounting partition %s on %s" %
                          (partition.path, "/target/home"))
                    fs = partition.type
                    os.system("mkdir -p /target/home")
                    self.do_mount(partition.path, "/target/home", fs, None)
                    # if reusing a btrfs with /@home already being there wont
                    # currently just keep it; data outside of /@home will still
                    # be there (just not reachable from the mounted /@home subvolume)
                    os.system("btrfs subvolume create /target/home/@home")
                    #os.system("btrfs subvolume list -p /target/home")
                    print(" ------- Umount btrfs to remount subvolume /@home")
                    os.system("umount --force /target/home")
                    self.do_mount(partition.path, "/target/home",
                                  fs, "subvol=@home")
                    break

        # Mount the other partitions
        for partition in self.setup.partitions:
            if(partition.mount_as == "/@home" or partition.mount_as == "/@"):
                # already mounted as subvolume
                continue

            if(partition.mount_as is not None and partition.mount_as != "" and partition.mount_as != "/" and partition.mount_as != "swap"):
                print(" ------ Mounting %s on %s" %
                      (partition.path, "/target" + partition.mount_as))
                os.system("mkdir -p /target" + partition.mount_as)
                if partition.type == "fat16" or partition.type == "fat32":
                    fs = "vfat"
                else:
                    fs = partition.type
                self.do_mount(partition.path, "/target" +
                              partition.mount_as, fs, None)

    def get_blkid(self, path):
        uuid = path  # If we can't find the UUID we use the path
        blkid = subprocess.getoutput('blkid').split('\n')
        for blkid_line in blkid:
            blkid_elements = blkid_line.split(':')
            if blkid_elements[0] == path:
                blkid_mini_elements = blkid_line.split()
                for blkid_mini_element in blkid_mini_elements:
                    if "UUID=" in blkid_mini_element:
                        uuid = blkid_mini_element.replace('"', '').strip()
                        break
                break
        return uuid

    def write_fstab(self):
        # write the /etc/fstab
        print(" --> Writing fstab")
        # make sure fstab has default /proc and /sys entries
        if(not os.path.exists("/target/etc/fstab")):
            os.system(
                "echo \"#### Static Filesystem Table File\" > /target/etc/fstab")
        fstab = open("/target/etc/fstab", "a")
        fstab.write("proc\t/proc\tproc\tdefaults\t0\t0\n")
        if(not self.setup.skip_mount):
            if self.setup.automated:
                fstab.write("# %s\n" % self.auto_root_partition)
                fstab.write("%s /  ext4 defaults 0 1\n" %
                            self.get_blkid(self.auto_root_partition))
                fstab.write("# %s\n" % self.auto_swap_partition)
                fstab.write("%s none   swap sw 0 0\n" %
                                self.get_blkid(self.auto_swap_partition))
                if (self.auto_boot_partition is not None):
                    fstab.write("# %s\n" % self.auto_boot_partition)
                    fstab.write("%s /boot  ext4 defaults 0 1\n" %
                                self.get_blkid(self.auto_boot_partition))
                if (self.auto_efi_partition is not None):
                    fstab.write("# %s\n" % self.auto_efi_partition)
                    fstab.write("%s /boot/efi  vfat defaults 0 1\n" %
                                self.get_blkid(self.auto_efi_partition))
            else:
                for partition in self.setup.partitions:
                    if (partition.mount_as is not None and partition.mount_as != "" and partition.mount_as != "None"):
                        fstab.write("# %s\n" % (partition.path))
                        if(partition.mount_as == "/"):
                            fstab_fsck_option = "1"
                        # section could be removed - just to state/document that fscheck is turned off
                        # intentionally with /@ (same would be true if btrfs used without a subvol)
                        # /bin/fsck.btrfs comment states to use fs-check==0 on mount
                        elif(partition.mount_as == "/@"):
                            fstab_fsck_option = "0"
                        else:
                            fstab_fsck_option = "0"

                        if("ext" in partition.type):
                            fstab_mount_options = "rw,errors=remount-ro"
                        elif partition.type == "btrfs" and partition.mount_as == "/@":
                            fstab_mount_options = "rw,subvol=/@"
                            # sort of dirty hack - we are done with subvol handling
                            # mount_as is next used to setup the mount point
                            partition.mount_as = "/"
                        elif partition.type == "btrfs" and partition.mount_as == "/@home":
                            fstab_mount_options = "rw,subvol=/@home"
                            # sort of dirty hack - see above
                            partition.mount_as = "/home"
                        else:
                            fstab_mount_options = "defaults"

                        if partition.type == "fat16" or partition.type == "fat32":
                            fs = "vfat"
                        else:
                            fs = partition.type

                        partition_uuid = self.get_blkid(partition.path)
                        if(fs == "swap"):
                            fstab.write("%s\tswap\tswap\tsw\t0\t0\n" %
                                        partition_uuid)
                        else:
                            fstab.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                                partition_uuid, partition.mount_as, fs, fstab_mount_options, "0", fstab_fsck_option))
        fstab.close()


    def finish_installation(self):
        # Steps:
        our_total = 11
        our_current = 4


        # set the locale
        print(" --> Yerel ayarlanıyor")
        our_current += 1
        self.update_progress(our_current, our_total, False,
                             False, ("Setting locale"))
        os.system("echo \"LC_COLLATE=C\" > /target/etc/env.d/02locale")
        os.system("echo \"LC_ALL=%s.UTF-8\" >> /target/etc/env.d/02locale" %
                  self.setup.language)
        self.update_progress(our_current, our_total, False,
                             False, ("Updating environment"))
        os.system("echo \"LANG=%s.UTF-8\" >> /target/etc/env.d/02locale" %
                  self.setup.language)
        self.do_run_in_chroot("cat /etc/env.d/* | grep -v \"^#\" > /etc/environment")


        # set the hostname
        print(" --> Bilgisayar adı ayarlanıyor")
        os.system("echo \"%s\" > /target/etc/hostname" % self.setup.hostname)

        # set the timezone
        print(" --> Zaman dilimi ayarlanıyor")
        os.system("echo \"%s\" > /target/etc/timezone" % self.setup.timezone)
        os.system("rm -f /target/etc/localtime")
        os.system("ln -s /usr/share/zoneinfo/%s /target/etc/localtime" %
                  self.setup.timezone)

        # set the keyboard options..
        print(" --> Klavye ayarlanıyor")
        our_current += 1
        self.update_progress(our_current, our_total, False,
                             False, ("Setting keyboard options"))
        #Keyboard settings openrc
        newconsolefh = open("/target/etc/conf.d/keymaps", "w")
        if not self.setup.keyboard_layout:
            self.setup.keyboard_layout="en"
        if not self.setup.keyboard_variant:
            self.setup.keyboard_variant=""
        newconsolefh.write("keymap=\"{}{}\"\n".format(self.setup.keyboard_layout,self.setup.keyboard_variant))
        newconsolefh.close()
        #Keyboard settings X11
        self.update_progress(our_current, our_total, False,
                             False, ("Settings X11 keyboard options"))
        
        newconsolefh = open("/target/etc/X11/xorg.conf.d/10-keyboard.conf", "w")
        newconsolefh.write('Section "InputClass"\n')
        newconsolefh.write('Identifier "system-keyboard"\n')
        newconsolefh.write('MatchIsKeyboard "on"\n')
        newconsolefh.write('Option "XkbLayout" "{}"\n'.format(self.setup.keyboard_layout))
        newconsolefh.write('Option "XkbModel" "{}"\n'.format(self.setup.keyboard_model))
        newconsolefh.write('Option "XkbVariant" "{}"\n'.format(self.setup.keyboard_variant))
        newconsolefh.write('#Option "XkbOptions" "grp:alt_shift_toggle"\n')
        newconsolefh.write('EndSection\n')
        newconsolefh.close()


         
        # write MBR (grub)
        print(" --> Grub Ayarlanıyor")
        our_current += 1
        if(self.setup.grub_device is not None):
            self.update_progress(our_current, our_total,
                                 False, False, ("Installing bootloader"))
            print(" --> Running grub-install")
            self.do_run_in_chroot("grub-install --force %s" %
                                  self.setup.grub_device)
            self.update_progress(our_current, our_total, False,
                             False, ("Configuring bootloader"))
        
            # fix not add windows grub entry
            self.do_run_in_chroot("update-grub")
            self.do_configure_grub(our_total, our_current)
            grub_retries = 0
            while (not self.do_check_grub(our_total, our_current)):
                self.do_configure_grub(our_total, our_current)
                grub_retries = grub_retries + 1
                if grub_retries >= 5:
                    self.error_message(message=(
                        "WARNING: The grub bootloader was not configured properly! You need to configure it manually."))
                    break


        # now unmount it
        print(" --> Bölümler ayrılıyor")
        self.update_progress(our_current, our_total, False,
                             False, ("Unmounting Partitions"))
        
        os.system("umount --force /target/dev/shm")
        os.system("umount --force /target/dev/pts")
        if self.setup.gptonefi:
            os.system("umount --force /target/boot/efi")
            os.system("umount --force /target/media/cdrom")
        os.system("umount --force /target/boot")
        os.system("umount --force /target/dev/")
        os.system("umount --force /target/sys/")
        os.system("umount --force /target/proc/")
        os.system("umount --force /target/run/")
        os.system("rm -f /target/etc/resolv.conf")
        os.system("mv /target/etc/resolv.conf.bk /target/etc/resolv.conf")
        if(not self.setup.skip_mount):
            for partition in self.setup.partitions:
                if(partition.mount_as is not None and partition.mount_as != "" and partition.mount_as != "/" and partition.mount_as != "swap"):
                    self.do_unmount("/target" + partition.mount_as)
            self.do_unmount("/target")
        self.do_unmount("/source")

        self.update_progress(0, 0, False, True, ("Installation finished"))
        print(" --> All done")

    def do_run_in_chroot(self, command):
        command = command.replace('"', "'").strip()
        print("chroot /target/ /bin/sh -c \"%s\"" % command)
        os.system("chroot /target/ /bin/sh -c \"%s\"" % command)

    def do_configure_grub(self, our_total, our_current):
        self.update_progress(our_current, our_total, True,
                             False, ("Configuring bootloader"))
        print(" --> Running grub-mkconfig")
        self.do_run_in_chroot("grub-mkconfig -o /boot/grub/grub.cfg")
        grub_output = subprocess.getoutput(
            "chroot /target/ /bin/sh -c \"grub-mkconfig -o /boot/grub/grub.cfg\"")
        grubfh = open("/var/log/live-installer-grub-output.log", "w")
        grubfh.writelines(grub_output)
        grubfh.close()

    def do_check_grub(self, our_total, our_current):
        self.update_progress(our_current, our_total, True,
                             False, ("Checking bootloader"))
        print(" --> Checking Grub configuration")
        time.sleep(5)
        if os.path.exists("/target/boot/grub/grub.cfg"):
            return True
        else:
            print("!No /target/boot/grub/grub.cfg file found!")
            return False

    def do_mount(self, device, dest, type, options=None):
        ''' Mount a filesystem '''
        p = None
        if(options is not None):
            cmd = "mount -o %s -t %s %s %s" % (options, type, device, dest)
        else:
            cmd = "mount -t %s %s %s" % (type, device, dest)
        print("EXECUTING: '%s'" % cmd)
        self.exec_cmd(cmd)

    def do_unmount(self, mountpoint):
        ''' Unmount a filesystem '''
        cmd = "umount %s" % mountpoint
        print("EXECUTING: '%s'" % cmd)
        self.exec_cmd(cmd)

    # Execute schell command and return output in a list
    def exec_cmd(self, cmd):
        return os.system(cmd)


    def run_preinstall(self):
        os.system("bash /usr/lib/live-installer/scripts/preinstall.sh")
        
    def run_postinstall(self):
        os.system("cp /usr/lib/live-installer/scripts/postinstall.sh /target/tmp/script.sh")
        os.system("chroot /target /tmp/script.sh")
        os.system(" rm -f /target/tmp/script")
        
        
# Represents the choices made by the user


class Setup(object):
    language = None
    timezone = None
    keyboard_model = None
    keyboard_layout = None
    keyboard_variant = None
    partitions = []  # Array of PartitionSetup objects
    username = None
    hostname = None
    autologin = False
    ecryptfs = False
    password1 = None
    password2 = None
    real_name = None
    grub_device = None
    disks = []
    automated = True
    disk = None
    diskname = None
    passphrase1 = None
    passphrase2 = None
    lvm = False
    luks = False
    badblocks = False
    target_disk = None
    gptonefi = False
    # Optionally skip all mouting/partitioning for advanced users with custom setups (raid/dmcrypt/etc)
    # Make sure the user knows that they need to:
    #  * Mount their target directory structure at /target
    #  * NOT mount /target/dev, /target/dev/shm, /target/dev/pts, /target/proc, and /target/sys
    #  * Manually create /target/etc/fstab after start_installation has completed and before finish_installation is called
    #  * Install cryptsetup/dmraid/mdadm/etc in target environment (using chroot) between start_installation and finish_installation
    #  * Make sure target is mounted using the same block device as is used in /target/etc/fstab (eg if you change the name of a dm-crypt device between now and /target/etc/fstab, update-initramfs will likely fail)
    skip_mount = False

    # Descriptions (used by the summary screen)
    keyboard_model_description = None
    keyboard_layout_description = None
    keyboard_variant_description = None

    def print_setup(self):
        if True:
            print(
                "-------------------------------------------------------------------------")
            print("language: %s" % self.language)
            print("timezone: %s" % self.timezone)
            print("keyboard: %s - %s (%s) - %s - %s (%s)" % (self.keyboard_model, self.keyboard_layout, self.keyboard_variant,
                                                             self.keyboard_model_description, self.keyboard_layout_description, self.keyboard_variant_description))
            print("user: %s (%s)" % (self.username, self.real_name))
            print("autologin: ", self.autologin)
            print("ecryptfs: ", self.ecryptfs)
            print("hostname: %s " % self.hostname)
            print("passwords: %s - %s" % (self.password1, self.password2))
            print("grub_device: %s " % self.grub_device)
            print("skip_mount: %s" % self.skip_mount)
            print("automated: %s" % self.automated)
            if self.automated:
                print("disk: %s (%s)" % (self.disk, self.diskname))
                print("luks: %s" % self.luks)
                print("badblocks: %s" % self.badblocks)
                print("lvm: %s" % self.lvm)
                print("passphrase: %s - %s" %
                      (self.passphrase1, self.passphrase2))
            if (not self.skip_mount):
                print("target_disk: %s " % self.target_disk)
                if self.gptonefi:
                    print("GPT partition table: True")
                else:
                    print("GPT partition table: False")
                print("disks: %s " % self.disks)
                print("partitions:")
                for partition in self.partitions:
                    partition.print_partition()
            print(
                "-------------------------------------------------------------------------")

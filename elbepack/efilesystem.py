# ELBE - Debian Based Embedded Rootfilesystem Builder
# Copyright (c) 2015-2017 Manuel Traut <manut@linutronix.de>
# Copyright (c) 2016-2017 Torben Hohn <torben.hohn@linutronix.de>
# Copyright (c) 2017 Philipp Arras <philipp.arras@linutronix.de>
# Copyright (c) 2018 Martin Kaistra <martin.kaistra@linutronix.de>
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
import shutil
import subprocess
import io
import stat


from elbepack.asciidoclog import CommandError
from elbepack.filesystem import Filesystem
from elbepack.version import elbe_version
from elbepack.hdimg import do_hdimg
from elbepack.fstab import fstabentry
from elbepack.licencexml import copyright_xml
from elbepack.packers import default_packer
from elbepack.shellhelper import system


def copy_filelist(src, filelist, dst):
    for f in filelist:
        f = f.rstrip("\n")
        if src.isdir(f) and not src.islink(f):
            if not dst.isdir(f):
                dst.mkdir(f)
            st = src.stat(f)
            dst.chown(f, st.st_uid, st.st_gid)
        else:
            subprocess.call(["cp", "-a", "--reflink=auto",
                             src.fname(f), dst.fname(f)])
    # update utime which will change after a file has been copied into
    # the directory
    for f in filelist:
        f = f.rstrip("\n")
        if src.isdir(f) and not src.islink(f):
            shutil.copystat(src.fname(f), dst.fname(f))


def extract_target(src, xml, dst, log, cache):

    # pylint: disable=too-many-locals
    # pylint: disable=too-many-branches

    # create filelists describing the content of the target rfs
    if xml.tgt.has("tighten") or xml.tgt.has("diet"):
        pkglist = [n.et.text for n in xml.node(
            'target/pkg-list') if n.tag == 'pkg']
        arch = xml.text("project/buildimage/arch", key="arch")

        if xml.tgt.has("diet"):
            withdeps = []
            for p in pkglist:
                deps = cache.get_dependencies(p)
                withdeps += [d.name for d in deps]
                withdeps += [p]

            pkglist = list(set(withdeps))

        file_list = []
        for line in pkglist:
            file_list += src.cat_file("var/lib/dpkg/info/%s.list" % (line))
            file_list += src.cat_file("var/lib/dpkg/info/%s.conffiles" %
                                      (line))

            file_list += src.cat_file("var/lib/dpkg/info/%s:%s.list" %
                                      (line, arch))
            file_list += src.cat_file(
                "var/lib/dpkg/info/%s:%s.conffiles" %
                (line, arch))

        file_list = list(sorted(set(file_list)))
        copy_filelist(src, file_list, dst)
    else:
        # first copy most diretories
        for f in src.listdir():
            subprocess.call(["cp", "-a", "--reflink=auto", f, dst.fname('')])

    try:
        dst.mkdir_p("dev")
    except BaseException:
        pass
    try:
        dst.mkdir_p("proc")
    except BaseException:
        pass
    try:
        dst.mkdir_p("sys")
    except BaseException:
        pass

    if xml.tgt.has("setsel"):
        pkglist = [n.et.text for n in xml.node(
            'target/pkg-list') if n.tag == 'pkg']
        psel = 'var/cache/elbe/pkg-selections'

        with open(dst.fname(psel), 'w+') as f:
            for item in pkglist:
                f.write("%s  install\n" % item)

        host_arch = log.get_command_out("dpkg --print-architecture").strip()
        if xml.is_cross(host_arch):
            ui = "/usr/share/elbe/qemu-elbe/" + str(xml.defs["userinterpr"])
            if not os.path.exists(ui):
                ui = "/usr/bin/" + str(xml.defs["userinterpr"])
            log.do('cp %s %s' % (ui, dst.fname("usr/bin")))

        log.chroot(dst.path, "/usr/bin/dpkg --clear-selections")
        log.chroot(
            dst.path,
            "/usr/bin/dpkg --set-selections < %s " %
            dst.fname(psel))
        log.chroot(dst.path, "/usr/bin/dpkg --purge -a")


class ElbeFilesystem(Filesystem):
    def __init__(self, path, clean=False):
        Filesystem.__init__(self, path, clean)

    def dump_elbeversion(self, xml):
        f = self.open("etc/elbe_version", "w+")
        f.write("%s %s\n" % (xml.prj.text("name"), xml.prj.text("version")))
        f.write("this RFS was generated by elbe %s\n" % (elbe_version))
        f.write(time.strftime("%c\n"))
        f.close()

        version_file = self.open("etc/updated_version", "w")
        version_file.write(xml.text("/project/version"))
        version_file.close()

        elbe_base = self.open("etc/elbe_base.xml", "wb")
        xml.xml.write(elbe_base)
        self.chmod("etc/elbe_base.xml", stat.S_IREAD)

    def write_licenses(self, f, log, xml_fname=None):
        licence_xml = copyright_xml()
        for d in self.listdir("usr/share/doc/", skiplinks=True):
            try:
                with io.open(os.path.join(d, "copyright"), "rb") as lic:
                    lic_text = lic.read()
            except IOError as e:
                log.printo("Error while processing license file %s: '%s'" %
                           (os.path.join(d, "copyright"), e.strerror))
                lic_text = "Error while processing license file %s: '%s'" % (
                    os.path.join(d, "copyright"), e.strerror)

            lic_text = unicode(lic_text, encoding='utf-8', errors='replace')

            if f is not None:
                f.write(unicode(os.path.basename(d)))
                f.write(u":\n======================================"
                        "==========================================")
                f.write(u"\n")
                f.write(lic_text)
                f.write(u"\n\n")

            if xml_fname is not None:
                licence_xml.add_copyright_file(os.path.basename(d), lic_text)

        if xml_fname is not None:
            licence_xml.write(xml_fname)

class Excursion(object):

    RFS = {}

    @classmethod
    def begin(cls, rfs):
        cls.RFS[rfs.path] = []

    @classmethod
    def add(cls, rfs, path, restore=True, dst=None):
        cls.RFS[rfs.path].append(Excursion(path, restore, dst))

    @classmethod
    def do(cls, rfs):
        r = cls.RFS[rfs.path]
        for tmp in r:
            tmp._do_excursion(rfs)

    @classmethod
    def end(cls, rfs):
        r = cls.RFS[rfs.path]
        for tmp in r:
            if tmp.origin not in rfs.protect_from_excursion:
                tmp._undo_excursion(rfs)
            else:
                tmp._del_rfs_file(tmp._saved_to(), rfs)
        del r

    def __init__(self, path, restore, dst):
        self.origin = path
        self.restore = restore
        self.dst = dst

    def _saved_to(self):
        return "%s.orig" % self.origin

    def _do_excursion(self, rfs):
        if rfs.lexists(self.origin) and self.restore is True:
            save_to = self._saved_to()
            system('mv %s %s' % (rfs.fname(self.origin), rfs.fname(save_to)))
        if os.path.exists(self.origin):
            if self.dst is not None:
                dst = self.dst
            else:
                dst = self.origin
            system('cp %s %s' % (self.origin, rfs.fname(dst)))

    # This should be a method of rfs
    def _del_rfs_file(self, filename, rfs):
        if rfs.lexists(filename):
            flags = "-f"
            if rfs.isdir(filename):
                flags += "r"
            system("rm %s %s" % (flags, rfs.fname(filename)))

    def _undo_excursion(self, rfs):
        saved_to = self._saved_to()
        self._del_rfs_file(self.origin, rfs)
        if self.restore is True and rfs.lexists(saved_to):
            system('mv %s %s' % (rfs.fname(saved_to), rfs.fname(self.origin)))


class ChRootFilesystem(ElbeFilesystem):

    def __init__(self, path, interpreter=None, clean=False):
        ElbeFilesystem.__init__(self, path, clean)
        self.interpreter = interpreter
        self.cwd = os.open("/", os.O_RDONLY)
        self.inchroot = False
        self.protect_from_excursion = set()

    def __del__(self):
        os.close(self.cwd)

    def __enter__(self):
        Excursion.begin(self)
        Excursion.add(self, "/etc/resolv.conf")
        Excursion.add(self, "/etc/apt/apt.conf")
        Excursion.add(self, "/usr/sbin/policy-rc.d")

        if self.interpreter:
            if not self.exists("usr/bin"):
                if self.islink("usr/bin"):
                    Excursion.add(self, "/usr/bin")

            ui = "/usr/share/elbe/qemu-elbe/" + self.interpreter
            if not os.path.exists(ui):
                ui = "/usr/bin/" + self.interpreter

            Excursion.add(self, ui, False, "/usr/bin")

        Excursion.do(self)

        self.mkdir_p("usr/bin")
        self.mkdir_p("usr/sbin")
        self.write_file("usr/sbin/policy-rc.d", 0o755, "#!/bin/sh\nexit 101\n")
        self.mount()
        return self

    def __exit__(self, _typ, _value, _traceback):
        if self.inchroot:
            self.leave_chroot()
        self.umount()

        Excursion.end(self)
        self.protect_from_excursion = set()

    def protect(self, files):
        self.protect_from_excursion = files
        return self

    def mount(self):
        if self.path == '/':
            return
        try:
            system("mount -t proc none %s/proc" % self.path)
            system("mount -t sysfs none %s/sys" % self.path)
            system("mount -o bind /dev %s/dev" % self.path)
            system("mount -o bind /dev/pts %s/dev/pts" % self.path)
        except BaseException:
            self.umount()
            raise

    def enter_chroot(self):
        assert not self.inchroot

        os.environ["LANG"] = "C"
        os.environ["LANGUAGE"] = "C"
        os.environ["LC_ALL"] = "C"

        os.chdir(self.path)
        self.inchroot = True

        if self.path == '/':
            return

        os.chroot(self.path)

    def _umount(self, path):
        if os.path.ismount(path):
            system("umount %s" % path)

    def umount(self):
        if self.path == '/':
            return
        self._umount("%s/proc/sys/fs/binfmt_misc" % self.path)
        self._umount("%s/proc" % self.path)
        self._umount("%s/sys" % self.path)
        self._umount("%s/dev/pts" % self.path)
        self._umount("%s/dev" % self.path)

    def leave_chroot(self):
        assert self.inchroot

        os.fchdir(self.cwd)

        self.inchroot = False
        if self.path == '/':
            return

        os.chroot(".")


class TargetFs(ChRootFilesystem):
    def __init__(self, path, log, xml, clean=True):
        ChRootFilesystem.__init__(self, path, xml.defs["userinterpr"], clean)
        self.log = log
        self.xml = xml
        self.images = []
        self.image_packers = {}

    def write_fstab(self, xml):
        if not self.exists("etc"):
            if self.islink("etc"):
                self.mkdir(self.realpath("etc"))
            else:
                self.mkdir("etc")

        f = self.open("etc/fstab", "w")
        if xml.tgt.has("fstab"):
            for fs in xml.tgt.node("fstab"):
                if not fs.has("nofstab"):
                    fstab = fstabentry(xml, fs)
                    f.write(fstab.get_str())
            f.close()

    def part_target(self, targetdir, grub_version, grub_fw_type=None):

        # create target images and copy the rfs into them
        self.images = do_hdimg(
            self.log,
            self.xml,
            targetdir,
            self,
            grub_version,
            grub_fw_type)

        for i in self.images:
            self.image_packers[i] = default_packer

        if self.xml.has("target/package/tar"):
            targz_name = self.xml.text("target/package/tar/name")
            try:
                options = ''
                if self.xml.has("target/package/tar/options"):
                    options = self.xml.text("target/package/tar/options")
                cmd = "tar cfz %(dest)s/%(fname)s -C %(sdir)s %(options)s ."
                args = dict(
                    options=options,
                    dest=targetdir,
                    fname=targz_name,
                    sdir=self.fname('')
                )
                self.log.do(cmd % args)
                # only append filename if creating tarball was successful
                self.images.append(targz_name)
            except CommandError:
                # error was logged; continue creating cpio image
                pass

        if self.xml.has("target/package/cpio"):
            oldwd = os.getcwd()
            cpio_name = self.xml.text("target/package/cpio/name")
            os.chdir(self.fname(''))
            try:
                self.log.do(
                    "find . -print | cpio -ov -H newc >%s" %
                    os.path.join(
                        targetdir, cpio_name))
                # only append filename if creating cpio was successful
                self.images.append(cpio_name)
            except CommandError:
                # error was logged; continue
                pass

        if self.xml.has("target/package/squashfs"):
            oldwd = os.getcwd()
            sfs_name = self.xml.text("target/package/squashfs/name")
            os.chdir(self.fname(''))
            try:
                self.log.do(
                    "mksquashfs %s %s/%s -noappend -no-progress" %
                    (self.fname(''), targetdir, sfs_name))
                # only append filename if creating mksquashfs was successful
                self.images.append(sfs_name)
            except CommandError as e:
                # error was logged; continue
                pass

    def pack_images(self, builddir):
        for img, packer in self.image_packers.items():
            self.images.remove(img)
            packed = packer.pack_file(self.log, builddir, img)
            if packed:
                self.images.append(packed)


class BuildImgFs(ChRootFilesystem):
    def __init__(self, path, interpreter):
        ChRootFilesystem.__init__(self, path, interpreter)

#!/usr/bin/env python
#
# Copyright (C) 2015 Colin Walters <walters@verbum.org>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import sys
import re
import collections
import shutil
import subprocess
import tempfile
import yaml

from gi.repository import GLib, Gio

from .utils import log, fatal, run_sync, rmrf, ensuredir

def path_with_suffix(path, suffix):
    return os.path.dirname(path) + '/' + os.path.basename(path) + suffix

def make_absolute_url(parent, relpath):
    orig_parent = parent
    orig_relpath = relpath
    if parent.endswith('/'):
        parent = parent[0:-1]
    method_index = parent.find('://')
    assert method_index != -1
    first_slash = parent.find('/', method_index+3)
    assert first_slash != -1
    parent_path = parent[first_slash:]
    while relpath.startswith('../'):
        i = parent.rfind('/')
        if i == -1:
            fatal("Relative submodule path {0} is too long for parent {1}".format(orig_relpath, orig_parent))
        relpath = relpath[3:]
        parent = parent[0:i]
    parent = parent[0:first_slash] + parent
    if relpath == '':
        return parent
    return parent + '/' + relpath

GitSubmodule = collections.namedtuple('GitSubmodule',
                                      ['checksum', 'name', 'url'])

class GitMirror(object):
    _pathname_quote_re = re.compile(r'[/\.]')

    def __init__(self, mirrordir):
        self.mirrordir = mirrordir
        self.tmpdir = mirrordir + '/_tmp'
        self.gitconfig = mirrordir + '/.gitconfig'
        ensuredir(self.tmpdir)

    def _gitenv(self):
        return {'HOME': self.mirrordir}

    def _runv(self, argv, **kwargs):
        if 'env' not in kwargs:
            kwargs['env'] = {}
        kwargs['env'].update(self._gitenv())
        run_sync(['git'] + list(argv), **kwargs)

    def _run(self, *argv, **kwargs):
        self._runv(argv, **kwargs)

    def set_config(self, config):
        new = self.gitconfig + '.tmp'
        with open(config) as f:
            ygitconfig = yaml.load(f)
        with open(new, 'w') as f:
            f.write('# Automatically generated by rpmdistro-gitoverlay from gitconfig.yml; do not edit!\n')
            aliases = ygitconfig.get('aliases', [])
            for alias in aliases:
                f.write('[url "{0}"]\n  insteadof = {1}:\n'.format(alias['url'], alias['name']))
        os.rename(new, self.gitconfig)

    def _get_mirrordir(self, uri, prefix=''):
        colon = uri.find('://')
        if colon >= 0:
            scheme = uri[0:colon]
            rest = uri[colon+3:]
        else:
            raise Exception("Invalid uri {0}".format(uri))
        if prefix:
            prefix = prefix + '/'
        return self.mirrordir + '/' + prefix + scheme + '/' + rest

    def _git_revparse(self, gitdir, branch):
        return subprocess.check_output(['git', 'rev-parse', branch], cwd=gitdir).strip()

    def _strip_file_url(self, url):
        """Remove the file:// prefix, which causes git to fall back to a
        slower fetch process.

        """
        if url.startswith('file://'):
            return url[len('file://'):]
        else:
            return url

    def _list_submodules_in(self, checkout, uri, rev='HEAD'):
        self._run('checkout', '-q', '-f', rev, cwd=checkout)
        proc = subprocess.Popen(['git', 'submodule', 'status'], cwd=checkout,
                                stdout=subprocess.PIPE, env=self._gitenv())
        submodules = []
        for line in proc.stdout:
            line = line.strip()
            if line == '':
                continue
            line = line[1:]
            parts = line.split(' ', 2)
            if len(parts) < 2:
                continue
            sub_checksum, sub_name = parts[0:2]
            sub_url = subprocess.check_output(['git', 'config', '-f', '.gitmodules',
                                               'submodule.{0}.url'.format(sub_name)],
                                              cwd=checkout).strip()
            if sub_url.startswith('../'):
                sub_url = make_absolute_url(uri, sub_url)
            submodules.append(GitSubmodule(sub_checksum, sub_name, sub_url))
        return submodules

    def _list_submodules(self, gitdir, uri, branch):
        current_rev = self._git_revparse(gitdir, branch)
        tmpdir = tempfile.mkdtemp('', 'tmp-gitmirror', self.tmpdir)
        tmp_clone =  tmpdir + '/checkout'
        try:
            self._run('clone', '-q', '--no-checkout', gitdir, tmp_clone)
            return self._list_submodules_in(tmp_clone, uri, rev=branch)
        finally:
            rmrf(tmpdir)

    def mirror(self, url, branch_or_tag,
               fetch=False, fetch_continue=False):
        mirrordir = self._get_mirrordir(url)
        tmp_mirror = os.path.dirname(mirrordir) + '/' + os.path.basename(mirrordir) + '.tmp'
        did_update = False

        
        rmrf(tmp_mirror)
        if not os.path.isdir(mirrordir):
            self._run('clone', '--mirror', self._strip_file_url(url), tmp_mirror)
            self._run('config', 'gc.auto', '0', cwd=tmp_mirror)
            os.rename(tmp_mirror, mirrordir)
        elif fetch:
            sys.stdout.write(os.path.basename(mirrordir) + ': ')
            self._run('fetch', cwd=mirrordir)
        
        rev = subprocess.check_output(['git', 'rev-parse', branch_or_tag], cwd=mirrordir).strip()

        # Cache making it more efficient to remirror the same commit
        # multiple times
        cachepath = mirrordir + '/submodules-cache-stamp'
        if os.path.exists(cachepath):
            cached_rev = open(cachepath).read().strip()
            if cached_rev == rev:
                return rev

        for module in self._list_submodules(mirrordir, url, branch_or_tag):
            log("Processing {0}".format(module))
            self.mirror(module.url, module.checksum,
                        fetch=fetch, fetch_continue=fetch_continue)
        with open(cachepath + '.tmp', 'w') as f:
            f.write(rev + '\n')
        os.rename(cachepath + '.tmp', cachepath)
        return rev

    def _process_checkout_submodules(self, checkout, url):
        for module in self._list_submodules_in(checkout, url):
            sub_mirrordir = self._get_mirrordir(module.url)
            config_key = 'submodule.{0}.url'.format(module.name)
            run_sync(['git', 'config', '-f', '.gitmodules',
                      config_key, 'file://' + sub_mirrordir],
                     cwd=checkout)
            run_sync(['git', 'submodule', 'update', '--init', module.name], cwd=checkout)
            self._process_checkout_submodules(checkout + '/' + module.name, module.url)

    def checkout(self, url, branch_or_tag, dest):
        mirrordir = self._get_mirrordir(url)
        run_sync(['git', 'clone', '-s', '--origin', 'localmirror', mirrordir, dest])
        run_sync(['git', 'checkout', '-q', branch_or_tag], cwd=dest)
        self._process_checkout_submodules(dest, url)
        return dest

    def describe(self, url, branch_or_tag):
        mirrordir = self._get_mirrordir(url)
        description = subprocess.check_output(['git', 'describe', '--long', '--abbrev=40', '--always', branch_or_tag],
                                              cwd=mirrordir).strip()
        if len(description) == 40:
            return [None, description]
        else:
            rgdash = description.rfind('-g')
            assert rgdash >= 0
            return (description[0:rgdash], description[rgdash+2:])
        

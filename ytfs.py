#!/usr/bin/python3

"""
Main module of YTFS. Executing this module causes YTFS filesystem mount in given directory.
"""

import os
import sys
import stat
import errno
import math
from enum import Enum
from copy import deepcopy
from time import time
from argparse import ArgumentParser, HelpFormatter
from functools import wraps

from fuse import FUSE, FuseOSError, Operations

#from stor import YTStor
from actions import YTActions, YTStor

class fd_dict(dict):

    """``dict`` extension, which finds the lowest unused descriptor and associates it with an ``YTStor`` object."""

    def push(self, yts):

        """
        Search for, add and return new file descriptor.

        Parameters
        ----------
        yts : YTStor-obj or None
            ``YTStor`` object for which we want to allocate a descriptor or ``None``, if we allocate descriptor for a
            control file.
     
        Returns
        -------
        k : int
            File descriptor.
        """

        if not isinstance(yts, (YTStor, type(None))):
            raise TypeError("Expected YTStor object or None.")

        k = 0
        while k in self.keys():
            k += 1
        self[k] = yts

        return k


class YTFS(Operations):

    """
    Main YTFS class.

    Attributes
    ----------
    st : dict
        Dictionary that contains basic file attributes. Consult ``man 2 stat`` for reference.
    searches : dict
        Dictionary that is a main interface to data of idividual searches and their results (movies) stored by
        filesystem. Format:
        
          searches = {
              'search phrase 1':  YTActions({
                                       'tytul1': <YTStor obj>,
                                       'tytul2': <YTStor obj>,
                                       ...
                                      }),
              'search phrase 2':  YTActions({ ... }),
              ...
          }
        
        ``YTStor`` object stores all needed information about movie, not only multimedia data.

        Attention: for simplicity, file extensions are present only during directory listing. In all other operations
        extensions are dropped.
    fds : fd_dict
        ``fd_dict`` dictionary which links descriptors in use with corresponding ``YTStor`` objects.
        Key: descriptor.
        Value: ``YTStor`` object for given file.
    __sh_script : bytes
        Bytes returned during control file read (empty shell script). System has to have impression of executing
        something. The actual operation is performed by YTFS during control file opening.
    """

    st = {

        'st_mode': stat.S_IFDIR | 0o555,
        'st_ino': 0,
        'st_dev': 0,
        'st_nlink': 2,
        'st_uid': os.getuid(),
        'st_gid': os.getgid(),
        'st_size': 4096,
        'st_blksize': 512,
        'st_atime': 0,
        'st_mtime': 0,
        'st_ctime': 0
    }

    __sh_script = b"#!/bin/sh\n"

    def __init__(self):

        self.searches = dict()
        self.fds = fd_dict()

    class PathType(Enum):

        """
        Human readable representation of path type of given tuple identifier.

        Attributes
        ----------
        invalid : int
            Invalid path.
        main : int
            Main directory.
        subdir : int
            Subdirectory (search directory).
        file : int
            File (search result).
        ctrl : int
            Control file.
        """

        invalid = 0
        main = 1
        subdir = 2
        file = 3
        ctrl = 4

        @staticmethod
        def get(p):

            """
            Get path type.


            Parameters
            ----------
            p : str or tuple
                Path or tuple identifier.

            Returns
            -------
            path_type : PathType
                Path type as ``PathType`` enum.
            """

            try:
                p = YTFS._YTFS__pathToTuple(p) # try to convert, if p is string. nothing will happen if not.
            except TypeError:
                pass

            if not isinstance(p, tuple) or len(p) != 2 or not (isinstance(p[0], (str, type(None))) and isinstance(p[1], (str, type(None)))):
                return YTFS.PathType.invalid

            elif p[0] is None and p[1] is None:
                return YTFS.PathType.main

            elif p[0] and p[1] is None:
                return YTFS.PathType.subdir

            elif p[0] and p[1]:
                
                if p[1][0] == ' ':
                    return YTFS.PathType.ctrl
                else:
                    return YTFS.PathType.file

            else:
                return YTFS.PathType.invalid

    class PathConvertError(Exception):
        pass

    def __pathToTuple(self, path):

        """
        Convert directory or file path to its tuple identifier.

        Parameters
        ----------
        path : str
            Path to convert. It can look like /, /directory, /directory/ or /directory/filename.

        Returns
        -------
        tup_id : tuple
            Two element tuple identifier of directory/file of (`directory`, `filename`) format. If path leads to main
            directory, then both fields of tuple will be ``None``. If path leads to a directory, then field `filename`
            will be ``None``.

        Raises
        ------
        YTFS.PathConvertError
            When invalid path is given.
        """

        if not path or path.count('/') > 2:
            raise YTFS.PathConvertError("Bad path given") # empty or too deep path

        try:
            split = path.split('/')
        except (AttributeError, TypeError):
            raise TypeError("Path has to be string") #path is not a string

        if split[0]:
            raise YTFS.PathConvertError("Path needs to start with '/'") # path doesn't start with '/'.
        del split[0]

        try:
            if not split[-1]: split.pop() # given path ended with '/'.
        except IndexError:
            raise YTFS.PathConvertError("Bad path given") # at least one element in split should exist at the moment

        if len(split) > 2:
            raise YTFS.PathConvertError("Path is too deep. Max allowed level is 2") # should happen due to first check, but ...

        try:
            d = split[0]
        except IndexError:
            d = None
        try:
            f = split[1]
        except IndexError:
            f = None

        if not d and f:
            raise YTFS.PathConvertError("Bad path given") # filename is present, but directory is not #sheeeeeeiiit

        return (d, f)

    def __exists(self, p):

        """
        Check if file of given path exists.

        Parameters
        ----------
        p : str or tuple
            Path or tuple identifier.

        Returns
        -------
        exists : bool
            ``True``, if file exists. Otherwise ``False``.
        """

        try:
            p = self.__pathToTuple(p)
        except TypeError:
            pass

        return ((not p[0] and not p[1]) or (p[0] in self.searches and not p[1]) or (p[0] in self.searches and
            p[1] in self.searches[p[0]]))

    def _pathdec(method):

        """
        Decorator that replaces string `path` argument with its tuple identifier.

        Parameters
        ----------
        method : function
            Function/method to decorate.

        Returns
        -------
        mod : function
            Function/method after decarotion.
        """

        @wraps(method) # functools.wraps makes docs autogeneration easy and proper for decorated functions.
        def mod(self, path, *args):

            try:
                return method(self, self.__pathToTuple(path), *args)

            except YTFS.PathConvertError:
                raise FuseOSError(errno.EINVAL)

        return mod

    @_pathdec
    def getattr(self, tid, fh=None):

        """
        File attributes.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        fh : int
            File descriptor. Unnecessary, therefore ignored.

        Returns
        -------
        st : dict
            Dictionary that contains file attributes. See: ``man 2 stat``.
        """

        if not self.__exists(tid):
            raise FuseOSError(errno.ENOENT)

        pt = self.PathType.get(tid)

        st = deepcopy(self.st)
        st['st_atime'] = int(time())
        st['st_mtime'] = st['st_atime']
        st['st_ctime'] = st['st_atime']

        if pt is self.PathType.file:
            
            st['st_mode'] = stat.S_IFREG | 0o444
            st['st_nlink'] = 1

            st['st_size'] = self.searches[ tid[0] ][ tid[1] ].filesize

        elif pt is self.PathType.ctrl:

            st['st_mode'] = stat.S_IFREG | 0o555 # correct? (FIXME?)
            st['st_nlink'] = 1
            st['st_size'] = len(self.__sh_script)

        st['st_blocks'] = math.ceil(st['st_size'] / st['st_blksize'])

        return st

    @_pathdec
    def readdir(self, tid, fh):

        """
        Read directory contents. Lists visible elements of ``YTActions`` object.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        fh : int
            File descriptor. Ommited in the function body.

        Returns
        -------
        list
            List of filenames, wich will be shown as directory content.
        """

        ret = []
        pt = self.PathType.get(tid)
        try:
            if pt is self.PathType.main:
                ret = list(self.searches)

            elif pt is self.PathType.subdir:
                ret = list(self.searches[tid[0]])

            elif pt is self.PathType.file:
                raise FuseOSError(errno.ENOTDIR)

            else:
                raise FuseOSError(errno.ENOENT)

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return ['.', '..'] + ret

    @_pathdec
    def mkdir(self, tid, mode):

        """
        Directory creation. Search is performed.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        mode : int
            Ignored.
        """

        pt = self.PathType.get(tid)

        if pt is self.PathType.invalid or pt is self.PathType.file:
            raise FuseOSError(errno.EPERM)

        if self.__exists(tid):
            raise FuseOSError(errno.EEXIST)

        self.searches[tid[0]] = YTActions(tid[0])
        self.searches[tid[0]].updateResults()

        return 0

    @_pathdec
    def rename(self, old, new):

        """
        Directory renaming support. Needed because many file managers create directories with default names, wich
        makes it impossible to perform a search without CLI. Name changes for files are not allowed, only for
        directories.

        Parameters
        ----------
        old : str
            Old name. Converted to tuple identifier by ``_pathdec`` decorator.
        new : str
            New name. Converted to tuple identifier in actual function body.
        """

        new = self.__pathToTuple(new)

        if not self.__exists(old):
            raise FuseOSError(errno.ENOENT)

        if self.PathType.get(old) is not self.PathType.subdir or self.PathType.get(new) is not self.PathType.subdir:
            raise FuseOSError(errno.EPERM)

        if self.__exists(new):
            raise FuseOSError(errno.EEXIST)

        self.searches[new[0]] = YTActions(new[0])
        self.searches[new[0]].updateResults()
        
        try:
            del self.searches[old[0]]

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return 0

    @_pathdec
    def rmdir(self, tid):

        """
        Directory removal. ``YTActions`` object under `tid` is told to clean all data, and then it is deleted.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        """

        pt = self.PathType.get(tid)

        if pt is self.PathType.main:
            raise FuseOSError(errno.EINVAL)
        elif pt is not self.PathType.subdir:
            raise FuseOSError(errno.ENOTDIR)

        try:
            self.searches[tid[0]].clean()
            del self.searches[tid[0]]

        except KeyError:
            raise FuseOSError(errno.ENOENT)

        return 0

    @_pathdec
    def unlink(self, tid):

        """
        File removal. In fact nothing is deleted, but for correct ``rm -r`` handling we deceive shell, that function
        has succeed.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        """

        return 0

    @_pathdec
    def open(self, tid, flags):

        """
        File open. ``YTStor`` object associated with this file is initialised and written to ``self.fds``.
        
        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        flags : int
            File open mode. Read-only access is allowed.

        Returns
        -------
        int
            New file descriptor
        """

        pt = self.PathType.get(tid)

        if pt is not self.PathType.file and pt is not self.PathType.ctrl:
            raise FuseOSError(errno.EINVAL)

        if flags & os.O_WRONLY or flags & os.O_RDWR:
            raise FuseOSError(errno.EROFS)

        if not self.__exists(tid):
            raise FuseOSError(errno.ENOENT)

        try:
            yts = self.searches[tid[0]][tid[1]]

            if yts.obtainInfo(): #FIXME coz it's ugly.
                fh = self.fds.push(yts)
                yts.registerHandler(fh)
                return fh
            else:
                raise FuseOSError(errno.EINVAL)

        except KeyError:
            return self.fds.push(None) # for control file no association is needed.

    @_pathdec
    def read(self, tid, length, offset, fh):

        """
        Read from a file. Data is obtained from ``YTStor`` object (which is kept under `fh` descriptor) using its
        ``read`` method.

        Parameters
        ----------
        tid : str
            Path to file. Original `path` argument is converted to tuple identifier by ``_pathdec`` decorator.
        length : int
            Length of data to read.
        offset : int
            Posision from which reading will start.
        fh : int
            File descriptor.

        Returns
        -------
        bytes
            Movie data.
        """

        try:
            return self.fds[fh].read(offset, length, fh)

        except AttributeError: # control file

            if tid[1] == " next":
                d = True
            elif tid[1] == " prev":
                d = False
            else:
                d = None

            try:
                self.searches[tid[0]].updateResults(d)
            except KeyError:
                raise FuseOSError(errno.EINVAL) # sth went wrong...

            return self.__sh_script[offset:offset+length]

        except KeyError: # descriptor does not exist.
            raise FuseOSError(errno.EBADF)

    @_pathdec
    def release(self, tid, fh):

        """
        Close file. Descriptor is removed from ``self.fds``.

        Parameters
        ----------
        tid : str
            Path to file. Ignored.
        fh : int
            File descriptor to release.
        """

        try:
            del self.fds[fh]
        except KeyError:
            raise FuseOSError(errno.EBADF)

        return 0


def main(mountpoint, debug):
    FUSE(YTFS(), mountpoint, foreground=debug)

if __name__ == '__main__':
    
    parser = ArgumentParser(description="YTFS - YouTube Filesystem: search and play materials from YouTube using filesystem operations.", epilog="Avoid mixing conflicting -a/-v flags with --format unless you know what you're doing - it might render files unplayable!\nTo download both audio and video data, provide either suitable format (e.g. 'best'; streaming is supported) or -a and -v flags together (whole data is usually downloaded before playing).", formatter_class=lambda prog: HelpFormatter(prog, max_help_position=50))
    parser.add_argument('mountpoint', type=str, nargs=1, help="Mountpoint")
    parser.add_argument('-a', action='store_true', default=False, help="Download audio (default)")
    parser.add_argument('-v', action='store_true', default=False, help="Download video")
    parser.add_argument('-r', action='store_true', default=False, help="RickRoll flag")
    parser.add_argument('-f', '--format', default=None, help="Set preferred format (exactly like in YoutubeDL). In case of error, YTFS falls back to 'bestvideo[height<=?1080]+bestaudio/best' without any warning!")
    s_grp = parser.add_mutually_exclusive_group()
    s_grp.add_argument('-s', action='store_true', default=False, help="Enable streaming whenever available.")
    s_grp.add_argument('-S', action='store_true', default=False, help="Always download whole data before reading.")
    parser.add_argument('-d', action='store_true', default=False, help="debug: run in foreground")

    x = parser.parse_args()

    av = 0b00
    if x.a: YTStor.SET_AV |= YTStor.DL_AUD
    if x.v: YTStor.SET_AV |= YTStor.DL_VID

    if x.r: YTStor.RICKASTLEY = True

    YTStor.SET_FMT = x.format

    if x.s: YTStor.SET_STREAM = True
    if x.S: YTStor.SET_STREAM = False

    # do I need to make staticmethod's to set those values? would seem like redundant code to me...
    # anyway, potential FIXME

    main(x.mountpoint[0], x.d)

#! /usr/bin/env python3
#
#  imapdedup.py
#
#  Looks for duplicate messages in a set of IMAP mailboxes and removes all but the first.
#  Comparison is normally based on the Message-ID header.
#
#  Default behaviour is purely to mark the duplicates as deleted.  Some mail clients
#  will allow you to view these and undelete them if you change your mind.
#
#  Copyright (c) 2013-2022 Quentin Stafford-Fraser.
#  All rights reserved, subject to the following:
#
#
#   This is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2 of the License, or
#   (at your option) any later version.
#
#   This software is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this software; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307,
#   USA.
#
import getpass
import hashlib
import imaplib
import os
import argparse
import re
import socket
import sys
import time
from typing import List, Dict, Tuple, Optional, Type, Any

from email.parser import BytesParser
from email.message import Message
from email.errors import HeaderParseError
from email.header import decode_header

# Increase the max line length that imaplib expects to get back from the server,
# since we're often dealing with big folders and large numbers of messages.
imaplib._MAXLINE = max(10_000_000, imaplib._MAXLINE)

class ImapDedupException(Exception):
    pass


def check_response(resp: Tuple[str, List[bytes]]):
    """
    IMAP responses should normally begin 'OK'. Strip that off, or raise
    an exception if it isn't there.
    """
    status, value = resp
    if status != "OK":
        raise ImapDedupException(f"Got response: {str(value)} from server")
    return value


def get_arguments(args: Optional[List[str]] = None) -> Tuple[argparse.Namespace, List[str]]:
    """
    Parse the given command-line arguments - defaults to using sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="Mark duplicate messages in IMAP mailboxes for deletion"
    )
    parser.add_argument(
        "-P", "--process", dest="process", help="IMAP process to access mailboxes"
    )
    parser.add_argument("-s", "--server", dest="server", help="IMAP server")
    parser.add_argument("-p", "--port", dest="port", help="IMAP server port", type=int)
    parser.add_argument("-x", "--ssl", dest="ssl", action="store_true", help="Use SSL")
    parser.add_argument("-X", "--starttls", dest="starttls", action="store_true", help="Require STARTTLS")
    parser.add_argument("-u", "--user", dest="user", help="IMAP user name")
    parser.add_argument("-a", "--authuser", dest='authuser', help='IMAP admin user')
    parser.add_argument(
        "-K",
        "--keyring",
        dest="keyring",
        nargs='?',
        const='',
        help="Keyring name to get password, no value means to use IMAP server name"
    )
    parser.add_argument(
        "-w",
        "--password",
        dest="password",
        help="IMAP password (Will prompt if not specified)",
    )
    parser.add_argument(
        "-v", "--verbose", dest="verbose", action="store_true", help="Verbose mode"
    )
    parser.add_argument(
        "-S", "--show", dest="show", action="store_true", help="Show duplicated messages"
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Don't actually do anything, just report what would be done",
    )
    parser.add_argument(
        "-c",
        "--checksum",
        dest="use_checksum",
        action="store_true",
        help="Use a checksum of several mail headers, instead of the Message-ID",
    )
    parser.add_argument(
        "-b",
        "--sentbefore",
        dest="sent_before",
        help="Only process messages sent before given date, given as d-m-y, e.g: 1-Feb-2020. Useful when there are many duplicates of each message",
    )
    parser.add_argument(
        "-m",
        "--checksum-with-id",
        dest="use_id_in_checksum",
        action="store_true",
        help="Include the Message-ID (if any) in the -c checksum.",
    )
    parser.add_argument(
        "--no-close",
        dest="no_close",
        action="store_true",
        help='Do not "close" mailbox when done. Some servers will purge deleted messages on a close command.',
    )
    parser.add_argument(
        "-l",
        "--list",
        dest="just_list",
        action="store_true",
        help="Just list mailboxes",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        dest="recursive",
        action="store_true",
        help="Remove duplicates recursively",
    )
    parser.add_argument(
        "-R",
        "--reverse",
        dest="reverse",
        action="store_true",
        help="Walk through the folders in reverse order",
    )
    parser.add_argument(
        "-t", "--only-tag", dest="tag_name",
        help="Tag duplicates with specified tag instead of deleting them"
    )
    parser.add_argument(
        "-y", "--copy", dest="copy_mailbox",
        help="Copy messages to specified mailbox before deleting them from current location."
    )
    parser.add_argument(
        "-d",
        "--delete",
        dest="delete_marked_messages",
        action="store_true",
        help="Delete marked messages (expunge)"
    )
    parser.add_argument('mailbox', nargs='*')

    options = parser.parse_args(args)
    mboxes = options.mailbox

    if (not options.server or not options.user) and not options.process:
        sys.stderr.write(
            "\nError: Must specify server, user, and at least one mailbox.\n\n"
        )
        parser.print_help()
        sys.exit(1)

    if options.recursive and len(mboxes) > 1:
        sys.stderr.write("\nError: You can only specify one mailbox if you use -r.\n")
        sys.exit(1)

    if options.use_id_in_checksum and not options.use_checksum:
        sys.stderr.write("\nError: If you use -m you must also use -c.\n")
        sys.exit(1)

    if options.keyring == '':
        options.keyring = options.server

    if options.keyring:
        import keyring
        options.password = keyring.get_password(options.keyring, options.user)

    if not options.password and not options.process:
        # Read from IMAPDEDUP_PASSWORD env variable, or prompt for one.
        options.password = os.getenv("IMAPDEDUP_PASSWORD") or getpass.getpass()

    return (options, mboxes)


# Thanks to http://www.doughellmann.com/PyMOTW/imaplib/
list_response_pattern = re.compile(
    rb'\((?P<flags>.*?)\) "(?P<delimiter>.*)" (?P<name>.*)'
)

def parse_list_response(line: bytes):
    if not isinstance(line, bytes):
        return None
    m = list_response_pattern.match(line)
    if m is None:
        sys.stderr.write(f"\nError: parsing list response '{line}'")
        sys.exit(1)
    flags, delimiter, mailbox_name = m.groups()
    mailbox_name = mailbox_name.strip(b'"')
    return (flags, delimiter, mailbox_name)

def str_header(parsed_message: Message, name: str) -> str:
    """
    Return the value (of the first instance, if more than one) of
    the given header, as a unicode string.
    """
    hdrlist = decode_header(parsed_message.get(name, ""))
    btext, charset = hdrlist[0]
    text = btext if isinstance(btext, str) else btext.decode("utf-8", "ignore")
    return text.lstrip()

def get_message_id(
    parsed_message: Message, options_use_checksum=False, options_use_id_in_checksum=False
) -> Optional[str]:
    """
    Normally, return the Message-ID header (or print a warning if it doesn't
    exist and return None).

    If options_use_checksum is specified, use md5 hash of several headers
    instead.

    For more safety, user should first do a dry run, reviewing them before
    deletion. Problems are extremely unlikely, but md5 is not collision-free.

    If options_use_id_in_checksum is specified, then the Message-ID will be
    included in the header checksum, otherwise it is excluded.
    """
    try:
        if options_use_checksum:
            md5 = hashlib.md5()
            sha = hashlib.sha256()
            sha3 = hashlib.sha3_256()
            def update(x):
                md5.update(x)
                sha.update(x)
                sha3.update(x)
            update(("From:" + str_header(parsed_message, "From")).encode())
            update(("To:" + str_header(parsed_message, "To")).encode())
            update(("Subject:" + str_header(parsed_message, "Subject")).encode())
            update(("Date:" + str_header(parsed_message, "Date")).encode())
            update(("Cc:" + str_header(parsed_message, "Cc")).encode())
            update(("Bcc:" + str_header(parsed_message, "Bcc")).encode())
            if options_use_id_in_checksum:
                update(("Message-ID:" + str_header(parsed_message, "Message-ID")).encode())
            msg_id = md5.hexdigest() + "|" + sha.hexdigest() + "|" + sha3.hexdigest()
        else:
            msg_id = str_header(parsed_message, "Message-ID")
            if not msg_id:
                print(
                    (
                        "Message '%s' dated '%s' has no Message-ID header."
                        % (
                            str_header(parsed_message, "Subject"),
                            str_header(parsed_message, "Date"),
                        )
                    )
                )
                print("You might want to use the -c option.")
                return None
        return msg_id.lstrip()

    except (ValueError, HeaderParseError):
        print(
            "WARNING: There was an exception trying to parse the headers of this message."
        )
        print("It may be corrupt, and you might consider deleting it.")
        print(
            (
                "Subject: %s\nFrom: %s\nDate: %s\n"
                % (
                    parsed_message["Subject"],
                    parsed_message["From"],
                    parsed_message["Date"],
                )
            )
        )
        print("Message skipped.")
        return None

def get_mailbox_list(server: imaplib.IMAP4, directory: str = '""', pattern: str = '"*"') -> List[str]:
    """
    Return a list of usable mailbox names which match the pattern.
    """
    resp = []
    for mb in check_response(server.list(directory, pattern)):
        if mb is None:
            continue
        bits = parse_list_response(mb)
        if rb"\\Noselect" not in bits[0]:
            resp.append(bits[2].decode())
    return resp

def get_matching_msgnums(server: imaplib.IMAP4, query: str, sent_before: Optional[str]) -> List[int]:
    """
    Return a list of ids of messages in the folder matching the given query.
    """
    resp = []
    if sent_before is not None:
        query = f"{query} SENTBEFORE {sent_before}"
        print(f"Getting matching messages sent before {sent_before}")
    deleted_info = check_response(server.search(None, query))
    if deleted_info and deleted_info[0]:   
        # If neither None nor empty nor [None], then
        # the first item should be a list of msg ids
        resp = [int(n) for n in deleted_info[0].split()]
    return resp

def get_deleted_msgnums(server: imaplib.IMAP4, sent_before: Optional[str]) -> List[int]:
    """
    Return a list of ids of deleted messages in the folder.
    """
    return get_matching_msgnums(server, "DELETED", sent_before)

def get_undeleted_msgnums(server: imaplib.IMAP4, sent_before: Optional[str]) -> List[int]:
    """
    Return a list of ids of non-deleted messages in the folder.
    """
    return get_matching_msgnums(server, "UNDELETED", sent_before)

def get_tagged_msgnums(server: imaplib.IMAP4, tag_name: str, sent_before: Optional[str]) -> List[int]:
    """
    Return a list of ids of tagged messages in the folder.
    """
    return get_matching_msgnums(server, f"KEYWORD {tag_name}", sent_before)

# Updated get_msg_headers with a retry mechanism and pause
def get_msg_headers(server: imaplib.IMAP4, msg_ids: List[int], retries: int = 3, pause: int = 5) -> List[Tuple[int, bytes]]:
    """
    Get the header bytes for each message in the provided list of IDs,
    with a retry mechanism if the fetch response is incomplete.
    Returns a list of tuples: (msgid, header_bytes).
    """
    message_ids_str = ",".join(map(str, msg_ids))
    for attempt in range(retries):
        try:
            ms = check_response(server.fetch(message_ids_str, "(RFC822.HEADER)"))
            # Check if we got the expected number of response parts (2 parts per message)
            if len(ms) < len(msg_ids) * 2:
                raise ValueError("Incomplete fetch response")
            break  # Successful fetch, exit retry loop.
        except Exception as e:
            print(f"Error fetching headers for messages {msg_ids[0]}-{msg_ids[-1]} (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(pause)
            else:
                raise
    headers = []
    count = min(len(ms) // 2, len(msg_ids))
    for ci in range(count):
        mnum = int(msg_ids[ci])
        header_tuple = ms[ci * 2]
        headers.append((mnum, header_tuple[1]))
    return headers

def print_message_info(parsed_message: Message):
    print("From: " + str_header(parsed_message, "From"))
    print("To: " + str_header(parsed_message, "To"))
    print("Cc: " + str_header(parsed_message, "Cc"))
    print("Bcc: " + str_header(parsed_message, "Bcc"))
    print("Subject: " + str_header(parsed_message, "Subject"))
    print("Date: " + str_header(parsed_message, "Date"))
    print("")

def add_quotes(mbox: str) -> str:
    if " " in mbox and (mbox[0] != '"' or mbox[-1] != '"'):
        mbox = '"' + mbox + '"'
    return mbox

def process(options, mboxes: List[str]):
    serverclass: Type[Any]
    if options.process:
        serverclass = imaplib.IMAP4_stream
    elif options.ssl:
        serverclass = imaplib.IMAP4_SSL
    else:
        serverclass = imaplib.IMAP4

    try:
        if options.process:
            server = serverclass(options.process)
        elif options.port:
            server = serverclass(options.server, options.port)
        else:
            server = serverclass(options.server)
    except socket.error as e:
        sys.stderr.write("\nFailed to connect to server. Might be host, port or SSL settings?\n")
        sys.stderr.write("%s\n\n" % e)
        sys.exit(1)

    if ("STARTTLS" in server.capabilities) and hasattr(server, "starttls"):
        server.starttls()
    elif options.starttls:
        sys.stderr.write("\nError: Server did not offer TLS\n")
        sys.exit(1)
    elif not options.ssl:
        sys.stderr.write("\nWarning: Unencrypted connection\n")

    try:
        if not options.process:
            if options.authuser:
                authcb = lambda resp: "{0}\x00{1}\x00{2}".format(options.user, options.authuser, options.password)
                server.authenticate("PLAIN", authcb)
            else:
                server.login(options.user, options.password)
    except Exception as e:
        sys.stderr.write("\nError: Login failed\n")
        sys.exit(1)

    if options.just_list:
        for mb in get_mailbox_list(server):
            print(mb)
        return

    if len(mboxes) == 0:
        sys.stderr.write("\nError: Must specify mailbox\n")
        sys.exit(1)

    if options.recursive:
        parent = add_quotes(mboxes[0])
        bits = parse_list_response(check_response(server.list(parent, '""'))[0])
        delimiter = bits[1].decode()
        pattern = '"' + delimiter + '*"'
        for mb in get_mailbox_list(server, parent, pattern):
            mboxes.append(mb)
        print("Working recursively from mailbox %s. There are %d total mailboxes." % (parent, len(mboxes)))

    if options.reverse:
        mboxes.reverse()

    if len(mboxes) > 1:
        print("Working with mailboxes in order: %s" % (", ".join(mboxes)))

    try:
        parser = BytesParser()
        msg_ids: Dict[str, str] = {}
        for mbox in mboxes:
            msgs_to_delete = []
            msg_map = {}

            mbox = add_quotes(mbox)
            msgs = check_response(server.select(mailbox=mbox, readonly=options.dry_run))[0]
            print("There are %d messages in %s." % (int(msgs), mbox))

            numdeleted = len(get_deleted_msgnums(server, options.sent_before))
            print(f'{numdeleted or "No"} message(s) currently marked as deleted in {mbox}')

            msgnums = get_undeleted_msgnums(server, options.sent_before)
            print(f"{len(msgnums)} others in {mbox}")

            chunkSize = 100
            if options.verbose:
                print("Reading the others... (in batches of %d)" % chunkSize)

            for i in range(0, len(msgnums), chunkSize):
                if options.verbose:
                    print("Batch starting at item %d" % i)
                try:
                    for mnum, hinfo in get_msg_headers(server, msgnums[i: i + chunkSize]):
                        if options.verbose:
                            print(f"Checking {mbox} message {mnum}")
                            # Save parsed message for verbose output
                            msg_map[mnum] = parser.parsebytes(hinfo)
                        mp = parser.parsebytes(hinfo)
                        msg_id = get_message_id(mp, options.use_checksum, options.use_id_in_checksum)
                        if msg_id:
                            if msg_id in msg_ids:
                                print("Message %s_%s is a duplicate of %s and %s be %s" % (
                                    mbox, mnum, msg_ids[msg_id],
                                    options.dry_run and "would" or "will",
                                    "tagged as '%s'" % options.tag_name if options.tag_name else "marked as deleted",
                                ))
                                if options.show or options.verbose:
                                    print("Subject: %s\nFrom: %s\nDate: %s\n" % (mp["Subject"], mp["From"], mp["Date"]))
                                msgs_to_delete.append(mnum)
                            else:
                                msg_ids[msg_id] = f"{mbox}_{mnum}"
                except Exception as e:
                    print(f"Error processing batch starting at item {i}: {e}")
                print(f"{min(len(msgnums), i + chunkSize)} message(s) in {mbox} processed")
                time.sleep(1)  # Short pause between batches to avoid overloading the server

            if not msgs_to_delete:
                print(f"No duplicates were found in {mbox}")
            else:
                if options.verbose:
                    print("These are the duplicate messages: ")
                    for mnum in msgs_to_delete:
                        print_message_info(msg_map[mnum])
                if options.dry_run:
                    print("If you had NOT selected the 'dry-run' option,\n  %i messages would now be %s." % (
                        len(msgs_to_delete),
                        "tagged as '%s'" % options.tag_name if options.tag_name else "marked as deleted",
                    ))
                else:
                    if options.copy_mailbox:
                        print("Copying %i messages to '%s'..." % (len(msgs_to_delete), options.copy_mailbox))
                    if options.tag_name:
                        print("Tagging %i messages as '%s'..." % (len(msgs_to_delete), options.tag_name))
                    else:
                        print("Marking %i messages as deleted..." % (len(msgs_to_delete)))
                    batch_size = 15
                    if options.verbose:
                        print("(in batches of %d)" % batch_size)
                    for i in range(0, len(msgs_to_delete), batch_size):

                        process_messages(server, msgs_to_delete[i: i + batch_size], options.tag_name, options.copy_mailbox)

                        if options.verbose:
                            print("Batch starting at item %d marked." % i)
                    print("Confirming new numbers...")
                    numdeleted = len(get_deleted_msgnums(server, options.sent_before))
                    numundel = len(get_undeleted_msgnums(server, options.sent_before))
                    print("There are now %s messages marked as deleted and %s others in %s." % (numdeleted, numundel, mbox))
                    if options.tag_name:
                        numtagged = len(get_tagged_msgnums(server, options.tag_name, options.sent_before))
                        print("There are now %s messages tagged as '%s' in %s." % (numtagged, options.tag_name, mbox))
            if options.delete_marked_messages:
                delete_marked_messages(server)
        if not options.no_close:
            server.close()
    except ImapDedupException as e:
        print("Error:", e, file=sys.stderr)
    finally:
        server.logout()

def process_messages(server: imaplib.IMAP4, msgs_to_delete: List[int], tag_name: Optional[str] = None,
                     copy_mailbox: Optional[str] = None, retries: int = 3, pause: int = 5):
    """
    Process the messages: either mark them as deleted or tag them,
    and copy them if a copy mailbox is specified.
    Retries the operation if an error occurs.
    """
    message_ids = ",".join(map(str, msgs_to_delete))
    action = tag_name or r"(\Deleted)"

    for attempt in range(1, retries + 1):
        try:
            if copy_mailbox:
                check_response(server.copy(message_ids, copy_mailbox))

            check_response(server.store(message_ids, "+FLAGS", action))
            time.sleep(1)  # Short pause between batches to avoid overloading the server
            break  # Exit the loop if operation succeeds
        except Exception as e:
            if attempt < retries:
                print(f"Attempt {attempt} failed: {e}. Retrying in {pause} seconds...")
                time.sleep(pause)
            else:
                print(f"Attempt {attempt} failed: {e}. No more retries left.")
                raise  # Re-raise the last exception if all retries fail

def delete_marked_messages(server: imaplib.IMAP4):
    print("Expunging deleted messages...")
    check_response(server.expunge())

def main():
    options, mboxes = get_arguments()
    process(options, mboxes)

if __name__ == "__main__":
    main()

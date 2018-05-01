from collections import namedtuple
import itertools
import json
import logging
import os
import socket
import re

import requests

from tornado import iostream
from tornado import ioloop
from tornado import httpclient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(HERE, "settings.json")) as settings_fp:
    settings = json.load(settings_fp)

irc_settings = settings["irc"]
slack_settings = settings["slack"]

NAME = irc_settings.get("nick", "wormhole")
HIGHLIGHTS = irc_settings.get("highlights", {})

Event = namedtuple("Event", "raw line source nick user host code args msg channel")

httpclient.AsyncHTTPClient.configure(None, defaults=dict(user_agent=NAME, validate_cert=False))
http_client = httpclient.AsyncHTTPClient()


def color_wrap(msg):
    def _wrap(match):
        colors = {
            "white": "00",
            "black": "01",
            "navy": "02",
            "green": "03",
            "red": "04",
            "brown": "05",
            "purple": "06",
            "orange": "07",
            "yellow": "08",
            "lime": "09",
            "teal": "10",
            "cyan": "11",
            "blue": "12",
            "pink": "13",
            "gray": "14",
            "silver": "15",
            "reset": "99"
        }
        word = match.groups()[0]
        color = colors.get(word)
        return "\x03%s" % color if color else word
    return re.sub(r'\${(\w+)}', _wrap, msg)


IRC_COMMAND_REGISTRY = {}


def command(name):
    def _inner(func):
        IRC_COMMAND_REGISTRY[name] = func
        return func
    return _inner


def get_highlights_for(channel):
    channel = channel.strip("#")
    for hl in itertools.chain(
            HIGHLIGHTS.get("all", []),
            HIGHLIGHTS.get(channel, [])):
        yield hl


def contains_highlight(event):
    msg = event.msg.lower()
    if event.code != 'PRIVMSG':
        return False
    for hl in get_highlights_for(event.channel):
        if isinstance(hl, list) and all(h in msg for h in hl):
            logger.debug(f"Found {hl}!")
            return True
        elif not isinstance(hl, list) and hl in msg:
            logger.debug(f"Found {hl}!")
            return True
    return False


@command("ping")
def ping_insights(irc, event):
    if contains_highlight(event):
        logger.debug("saw a highlight ping")
        response = requests.post(
            slack_settings["hook_url"],
            json={"text": f"({event.channel}) {event.nick} says: {event.msg}"})
        logger.debug(response.text)
        irc.most_recent_highlight_channel = event.channel
        irc.most_recent_highlight_nick = event.nick


class IRCClient:

    def __init__(self):
        self.io_loop = ioloop.IOLoop.current()
        self.address = irc_settings["server"]
        self.nick = irc_settings["nick"]
        self.user = irc_settings["nick"]
        self.port = irc_settings.get("port", 6667)
        self.sock_family = socket.AF_INET
        self.stream = None
        self.channels = set()
        self.most_recent_highlight_channel = None
        self.most_recent_highlight_nick = None

    def connect(self):
        if self.stream is not None:
            self.stream.close()

        sock = socket.socket(family=self.sock_family)
        sock.connect((self.address, self.port))
        self.stream = iostream.IOStream(sock, io_loop=self.io_loop)
        self.send_message("NICK " + self.nick)
        self.send_message("USER {0} 0 * :{0}".format(self.user))
        for channel in irc_settings["channels"]:
            self.join(channel)
        self.wait_for_input()

    def join(self, channel):
        self.channels.add("#" + channel)
        self.send_message("JOIN #%s" % (channel,))

    def wait_for_input(self):
        self.stream.read_until(b'\n', self._read_message)

    def _read_message(self, raw):
        raw = raw.rstrip(b'\r\n')

        line = raw.decode("utf-8")

        source = nick = user = host = None
        msg = line

        if line[0] == ":":
            pos = line.index(" ")
            source = line[1:pos]
            msg = line[pos + 1:]
            i = source.find("!")
            j = source.find("@")
            if i > 0 and j > 0:
                nick = source[:i]
                user = source[i + 1:j]
                host = source[j + 1:]

        sp = msg.split(" :", 1)
        code, *args = sp[0].split(" ")
        if len(sp) == 2:
            args.append(sp[1])

        event = Event(raw, line, source, nick, user, host,
                      code, args, args[-1], args[0] if '#' in args[0] else None)
        if event.channel and "MSG" in event.code:
            logger.debug("%s %s: %s", event.channel, event.nick, event.msg)
        self.handle_event(event)

    def handle_event(self, event):
        if event.code == "PING":
            self.send_message("PONG {0}".format(event.args[0]))
        else:
            self.dispatch(event)
        self.wait_for_input()

    def dispatch(self, event):
        for k, v in IRC_COMMAND_REGISTRY.items():
            self.io_loop.spawn_callback(v, self, event)

    def send_to_channel(self, channel, line):
        self.send_message("PRIVMSG {channel} :{line}".format(channel=channel, line=color_wrap(line)))

    def send_message(self, line):
        if isinstance(line, str):
            line = line.encode("utf-8")
        logger.debug("Sending: %s", line)
        self.stream.write(line + b"\r\n")

    def broadcast(self, msg):
        for channel in self.channels:
            self.send_to_channel(channel, msg)

    def pinger(self):

        def _handle_response(response):
            try:
                doc = json.loads(response.body)
            except Exception:
                logger.debug("No document from pinger")
            else:
                if doc:
                    self.send_from_slack(doc["user_name"][0],
                                         doc["text"][0])

        def _cb():
            http_client.fetch('%s/irc' % slack_settings["bot_url"],
                              _handle_response,
                              headers={'X-Wormhole': slack_settings["pinger-token"]})
        return _cb

    def send_from_slack(self, user, msg):
        if ":" in msg:
            channel, msg = msg.split(":", 1)
            channel = "#%s" % channel.strip("#")
            msg = msg.strip()
            if channel not in self.channels:
                logger.error(f"{user} requested to send a message to a channel I'm not connected to! ({channel})")
        elif self.most_recent_highlight_channel:
            channel, msg = self.most_recent_highlight_channel, msg
        else:
            logger.error(f"I don't know where to send the message from {user}")
            return

        decorated_message = f"[slack] @{user} said '{msg}'"
        self.send_to_channel(channel, decorated_message)

    def close(self):
        self.stream.close()


if __name__ == "__main__":
    loop = ioloop.PollIOLoop.current()
    logger.info("Starting IRC Client")
    bot = IRCClient()
    bot.connect()
    logger.info("Starting pinger")
    pinger = ioloop.PeriodicCallback(bot.pinger(), 5000)
    pinger.start()
    loop.start()


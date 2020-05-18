from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import zlib
import collections
import typing
import inspect
import weakref
import datetime

import ModuleUpdate

ModuleUpdate.update()

import websockets
import prompt_toolkit
from prompt_toolkit.patch_stdout import patch_stdout
from fuzzywuzzy import process as fuzzy_process

import Items
import Regions
import Utils
from Utils import get_item_name_from_id, get_location_name_from_address, ReceivedItem

console_names = frozenset(set(Items.item_table) | set(Regions.location_table))

CLIENT_PLAYING = 0
CLIENT_GOAL = 1


class Client:
    version: typing.List[int] = [0, 0, 0]
    tags: typing.List[str] = []

    def __init__(self, socket: websockets.server.WebSocketServerProtocol, ctx: Context):
        self.socket = socket
        self.auth = False
        self.name = None
        self.team = None
        self.slot = None
        self.send_index = 0
        self.tags = []
        self.version = [0, 0, 0]
        self.messageprocessor = ClientMessageProcessor(ctx, self)
        self.ctx = weakref.ref(ctx)

    async def disconnect(self):
        ctx = self.ctx()
        if ctx:
            await on_client_disconnected(ctx, self)
            ctx.clients.remove(self)

    @property
    def wants_item_notification(self):
        return self.auth and "FoundItems" in self.tags


class Context:
    def __init__(self, host: str, port: int, password: str, location_check_points: int, hint_cost: int,
                 item_cheat: bool, forfeit_mode: str = "disabled", remaining_mode: str = "disabled"):
        self.data_filename = None
        self.save_filename = None
        self.disable_save = False
        self.player_names = {}
        self.rom_names = {}
        self.remote_items = set()
        self.locations = {}
        self.host = host
        self.port = port
        self.password = password
        self.server = None
        self.countdown_timer = 0
        self.clients = []
        self.received_items = {}
        self.name_aliases: typing.Dict[typing.Tuple[int, int], str] = {}
        self.location_checks = collections.defaultdict(set)
        self.hint_cost = hint_cost
        self.location_check_points = location_check_points
        self.hints_used = collections.defaultdict(int)
        self.hints: typing.Dict[typing.Tuple[int, int], typing.Set[Utils.Hint]] = collections.defaultdict(set)
        self.forfeit_mode: str = forfeit_mode
        self.remaining_mode: str = remaining_mode
        self.item_cheat = item_cheat
        self.running = True
        self.client_activity_timers = {}
        self.client_game_state: typing.Dict[typing.Tuple[int, int], int] = collections.defaultdict(int)
        self.er_hint_data: typing.Dict[int, typing.Dict[int, str]] = {}
        self.commandprocessor = ServerCommandProcessor(self)

    def get_save(self) -> dict:
        d = {
            "rom_names": list(self.rom_names.items()),
            "received_items": tuple((k, v) for k, v in self.received_items.items()),
            "hints_used": tuple((key, value) for key, value in self.hints_used.items()),
            "hints": tuple((key, list(value)) for key, value in self.hints.items()),
            "location_checks": tuple((key, tuple(value)) for key, value in self.location_checks.items()),
            "name_aliases": tuple((key, value) for key, value in self.name_aliases.items()),
            "client_game_state": tuple((key, value) for key, value in self.client_game_state.items())
        }
        return d

    def set_save(self, savedata: dict):
        rom_names = savedata["rom_names"]
        received_items = {tuple(k): [ReceivedItem(*i) for i in v] for k, v in savedata["received_items"]}
        if not all([self.rom_names[tuple(rom)] == (team, slot) for rom, (team, slot) in rom_names]):
            raise Exception('Save file mismatch, will start a new game')
        self.received_items = received_items
        self.hints_used.update({tuple(key): value for key, value in savedata["hints_used"]})
        if "hints" in savedata:
            self.hints.update(
                {tuple(key): set(Utils.Hint(*hint) for hint in value) for key, value in savedata["hints"]})
        else:  # backwards compatiblity for <= 2.0.2
            old_hints = {tuple(key): set(value) for key, value in savedata["hints_sent"]}
            for team_slot, item_or_location_s in old_hints.items():
                team, slot = team_slot
                for item_or_location in item_or_location_s:
                    if item_or_location in Items.item_table:
                        hints = collect_hints(self, team, slot, item_or_location)
                    else:
                        hints = collect_hints_location(self, team, slot, item_or_location)
                    for hint in hints:
                        self.hints[team, hint.receiving_player].add(hint)
                        # even if it is the same hint, it won't be duped due to set
                        self.hints[team, hint.finding_player].add(hint)
        if "name_aliases" in savedata:
            self.name_aliases.update({tuple(key): value for key, value in savedata["name_aliases"]})
            if "client_game_state" in savedata:
                self.client_game_state.update({tuple(key): value for key, value in savedata["client_game_state"]})
        self.location_checks.update({tuple(key): set(value) for key, value in savedata["location_checks"]})
        logging.info(f'Loaded save file with {sum([len(p) for p in received_items.values()])} received items '
                     f'for {len(received_items)} players')

    def get_aliased_name(self, team: int, slot: int):
        if (team, slot) in self.name_aliases:
            return f"{self.name_aliases[team, slot]} ({self.player_names[team, slot]})"
        else:
            return self.player_names[team, slot]


async def send_msgs(client: Client, msgs):
    websocket = client.socket
    if not websocket or not websocket.open or websocket.closed:
        return
    try:
        await websocket.send(json.dumps(msgs))
    except websockets.ConnectionClosed:
        logging.exception("Exception during send_msgs")
        await client.disconnect()


async def send_json_msgs(client: Client, msg: str):
    websocket = client.socket
    if not websocket or not websocket.open or websocket.closed:
        return
    try:
        await websocket.send(msg)
    except websockets.ConnectionClosed:
        logging.exception("Exception during send_msgs")
        await client.disconnect()


def broadcast_all(ctx: Context, msgs):
    msgs = json.dumps(msgs)
    for client in ctx.clients:
        if client.auth:
            asyncio.create_task(send_json_msgs(client, msgs))


def broadcast_team(ctx: Context, team, msgs):
    msgs = json.dumps(msgs)
    for client in ctx.clients:
        if client.auth and client.team == team:
            asyncio.create_task(send_json_msgs(client, msgs))

def notify_all(ctx : Context, text):
    logging.info("Notice (all): %s" % text)
    broadcast_all(ctx, [['Print', text]])


def notify_team(ctx: Context, team: int, text: str):
    logging.info("Notice (Team #%d): %s" % (team + 1, text))
    broadcast_team(ctx, team, [['Print', text]])


def notify_client(client: Client, text: str):
    if not client.auth:
        return
    logging.info("Notice (Player %s in team %d): %s" % (client.name, client.team + 1, text))
    asyncio.create_task(send_msgs(client, [['Print', text]]))


# separated out, due to compatibilty between clients
def notify_hints(ctx: Context, team: int, hints: typing.List[Utils.Hint]):
    cmd = json.dumps([["Hint", hints]])  # make sure it is a list, as it can be set internally
    texts = [['Print', format_hint(ctx, team, hint)] for hint in hints]
    for _, text in texts:
        logging.info("Notice (Team #%d): %s" % (team + 1, text))
    texts = json.dumps(texts)
    for client in ctx.clients:
        if client.auth and client.team == team:
            if "Berserker" in client.tags and client.version >= [2, 2, 1]:
                payload = cmd
            else:
                payload = texts
            asyncio.create_task(send_json_msgs(client, payload))


def update_aliases(ctx: Context, team: int, client: typing.Optional[Client] = None):
    cmd = json.dumps([["AliasUpdate",
                       [(key[1], ctx.get_aliased_name(*key)) for key, value in ctx.player_names.items() if
                        key[0] == team]]])
    if client is None:
        for client in ctx.clients:
            if client.team == team and client.auth and client.version > [2, 0, 3]:
                asyncio.create_task(send_json_msgs(client, cmd))
    else:
        asyncio.create_task(send_json_msgs(client, cmd))


async def server(websocket, path, ctx: Context):
    client = Client(websocket, ctx)
    ctx.clients.append(client)

    try:
        await on_client_connected(ctx, client)
        async for data in websocket:
            for msg in json.loads(data):
                if len(msg) == 1:
                    cmd = msg
                    args = None
                else:
                    cmd = msg[0]
                    args = msg[1]
                await process_client_cmd(ctx, client, cmd, args)
    except Exception as e:
        if not isinstance(e, websockets.WebSocketException):
            logging.exception(e)
    finally:
        await client.disconnect()

async def on_client_connected(ctx: Context, client: Client):
    await send_msgs(client, [['RoomInfo', {
        'password': ctx.password is not None,
        'players': [(client.team, client.slot, ctx.name_aliases.get((client.team, client.slot), client.name)) for client
                    in ctx.clients if client.auth],
        # tags are for additional features in the communication.
        # Name them by feature or fork, as you feel is appropriate.
        'tags': ['Berserker'],
        'version': Utils._version_tuple
    }]])

async def on_client_disconnected(ctx: Context, client: Client):
    if client.auth:
        await on_client_left(ctx, client)


async def on_client_joined(ctx: Context, client: Client):
    notify_all(ctx,
               "%s (Team #%d) has joined the game. Client(%s, %s)." % (ctx.get_aliased_name(client.team, client.slot),
                                                                       client.team + 1,
                                                                       ".".join(str(x) for x in client.version),
                                                                       client.tags))

async def on_client_left(ctx: Context, client: Client):
    notify_all(ctx, "%s (Team #%d) has left the game" % (ctx.get_aliased_name(client.team, client.slot), client.team + 1))

async def countdown(ctx: Context, timer):
    notify_all(ctx, f'[Server]: Starting countdown of {timer}s')
    if ctx.countdown_timer:
        ctx.countdown_timer = timer  # timer is already running, set it to a different time
    else:
        ctx.countdown_timer = timer
        while ctx.countdown_timer > 0:
            notify_all(ctx, f'[Server]: {ctx.countdown_timer}')
            ctx.countdown_timer -= 1
            await asyncio.sleep(1)
        notify_all(ctx, f'[Server]: GO')

def get_players_string(ctx: Context):
    auth_clients = {(c.team, c.slot) for c in ctx.clients if c.auth}

    player_names = sorted(ctx.player_names.keys())
    current_team = -1
    text = ''
    for team, slot in player_names:
        player_name = ctx.player_names[team, slot]
        if team != current_team:
            text += f':: Team #{team + 1}: '
            current_team = team
        if (team, slot) in auth_clients:
            text += f'{player_name} '
        else:
            text += f'({player_name}) '
    return f'{len(auth_clients)} players of {len(ctx.player_names)} connected ' + text[:-1]


def get_received_items(ctx: Context, team: int, player: int) -> typing.List[ReceivedItem]:
    return ctx.received_items.setdefault((team, player), [])


def tuplize_received_items(items):
    return [(item.item, item.location, item.player) for item in items]


def send_new_items(ctx: Context):
    for client in ctx.clients:
        if not client.auth:
            continue
        items = get_received_items(ctx, client.team, client.slot)
        if len(items) > client.send_index:
            asyncio.create_task(send_msgs(client, [
                ['ReceivedItems', (client.send_index, tuplize_received_items(items)[client.send_index:])]]))
            client.send_index = len(items)


def forfeit_player(ctx: Context, team: int, slot: int):
    all_locations = {values[0] for values in Regions.location_table.values() if type(values[0]) is int}
    notify_all(ctx, "%s (Team #%d) has forfeited" % (ctx.player_names[(team, slot)], team + 1))
    register_location_checks(ctx, team, slot, all_locations)


def get_remaining(ctx: Context, team: int, slot: int) -> typing.List[int]:
    items = []
    for (location, location_slot) in ctx.locations:
        if location_slot == slot and location not in ctx.location_checks[team, slot]:
            items.append(ctx.locations[location, slot][0])  # item ID
    return sorted(items)

def register_location_checks(ctx: Context, team: int, slot: int, locations):
    ctx.client_activity_timers[team, slot] = datetime.datetime.now(datetime.timezone.utc)
    found_items = False
    for location in locations:
        if (location, slot) in ctx.locations:
            target_item, target_player = ctx.locations[(location, slot)]
            if target_player != slot or slot in ctx.remote_items:
                found = False
                recvd_items = get_received_items(ctx, team, target_player)
                for recvd_item in recvd_items:
                    if recvd_item.location == location and recvd_item.player == slot:
                        found = True
                        break

                if not found:
                    new_item = ReceivedItem(target_item, location, slot)
                    recvd_items.append(new_item)
                    if slot != target_player:
                        broadcast_team(ctx, team, [['ItemSent', (slot, location, target_player, target_item)]])
                    logging.info('(Team #%d) %s sent %s to %s (%s)' % (
                    team + 1, ctx.player_names[(team, slot)], get_item_name_from_id(target_item),
                    ctx.player_names[(team, target_player)], get_location_name_from_address(location)))
                    found_items = True
            elif target_player == slot:  # local pickup, notify clients of the pickup
                if location not in ctx.location_checks[team, slot]:
                    for client in ctx.clients:
                        if client.team == team and client.wants_item_notification:
                            asyncio.create_task(
                                send_msgs(client, [['ItemFound', (target_item, location, slot)]]))
    ctx.location_checks[team, slot] |= set(locations)
    send_new_items(ctx)

    if found_items:
        save(ctx)


def save(ctx: Context):
    if not ctx.disable_save:
        try:
            jsonstr = json.dumps(ctx.get_save())
            with open(ctx.save_filename, "wb") as f:
                f.write(zlib.compress(jsonstr.encode("utf-8")))
        except Exception as e:
            logging.exception(e)


def collect_hints(ctx: Context, team: int, slot: int, item: str) -> typing.List[Utils.Hint]:
    hints = []
    seeked_item_id = Items.item_table[item][3]
    for check, result in ctx.locations.items():
        item_id, receiving_player = result
        if receiving_player == slot and item_id == seeked_item_id:
            location_id, finding_player = check
            found = location_id in ctx.location_checks[team, finding_player]
            entrance = ctx.er_hint_data.get(finding_player, {}).get(location_id, "")
            hints.append(Utils.Hint(receiving_player, finding_player, location_id, item_id, found, entrance))

    return hints


def collect_hints_location(ctx: Context, team: int, slot: int, location: str) -> typing.List[Utils.Hint]:
    hints = []
    seeked_location = Regions.location_table[location][0]
    for check, result in ctx.locations.items():
        location_id, finding_player = check
        if finding_player == slot and location_id == seeked_location:
            item_id, receiving_player = result
            found = location_id in ctx.location_checks[team, finding_player]
            entrance = ctx.er_hint_data.get(finding_player, {}).get(location_id, "")
            hints.append(Utils.Hint(receiving_player, finding_player, location_id, item_id, found, entrance))
            break  # each location has 1 item
    return hints


def format_hint(ctx: Context, team: int, hint: Utils.Hint) -> str:
    text = f"[Hint]: {ctx.player_names[team, hint.receiving_player]}'s " \
           f"{Items.lookup_id_to_name[hint.item]} is " \
           f"at {get_location_name_from_address(hint.location)} " \
           f"in {ctx.player_names[team, hint.finding_player]}'s World"

    if hint.entrance:
        text += f" at {hint.entrance}"
    return text + (". (found)" if hint.found else ".")


def get_intended_text(input_text: str, possible_answers: typing.Iterable[str]= console_names) -> typing.Tuple[str, bool, str]:
    picks = fuzzy_process.extract(input_text, possible_answers, limit=2)
    if len(picks) > 1:
        dif = picks[0][1] - picks[1][1]
        if picks[0][1] == 100:
            return picks[0][0], True, "Perfect Match"
        elif picks[0][1] < 75:
            return picks[0][0], False, f"Didn't find something that closely matches, " \
                                       f"did you mean {picks[0][0]}? ({picks[0][1]}% sure)"
        elif dif > 5:
            return picks[0][0], True, "Close Match"
        else:
            return picks[0][0], False, f"Too many close matches, did you mean {picks[0][0]}? ({picks[0][1]}% sure)"
    else:
        if picks[0][1] > 90:
            return picks[0][0], True, "Only Option Match"
        else:
            return picks[0][0], False, f"Did you mean {picks[0][0]}? ({picks[0][1]}% sure)"


class CommandMeta(type):
    def __new__(cls, name, bases, attrs):
        commands = attrs["commands"] = {}
        for base in bases:
            commands.update(base.commands)
        commands.update({name[5:].lower(): method for name, method in attrs.items() if
                         name.startswith("_cmd_")})
        return super(CommandMeta, cls).__new__(cls, name, bases, attrs)


def mark_raw(function):
    function.raw_text = True
    return function


class CommandProcessor(metaclass=CommandMeta):
    commands: typing.Dict[str, typing.Callable]
    marker = "/"

    def output(self, text: str):
        print(text)

    def __call__(self, raw: str) -> typing.Optional[bool]:
        if not raw:
            return
        try:
            command = raw.split()
            basecommand = command[0]
            if basecommand[0] == self.marker:
                method = self.commands.get(basecommand[1:].lower(), None)
                if not method:
                    self._error_unknown_command(basecommand[1:])
                    return False
                else:
                    if getattr(method, "raw_text", False):  # method is requesting unprocessed text data
                        arg = raw.split(maxsplit=1)
                        if len(arg) > 1:
                            return method(self, arg[1])  # argument text was found, so pass it along
                        else:
                            return method(self)  # argument may be optional, try running without args
                    else:
                        return method(self, *command[1:])  # pass each word as argument
            else:
                self.default(raw)
        except Exception as e:
            self._error_parsing_command(e)

    def get_help_text(self) -> str:
        s = ""
        for command, method in self.commands.items():
            spec = inspect.signature(method).parameters
            argtext = ""
            for argname, parameter in spec.items():
                if argname == "self":
                    continue

                if isinstance(parameter.default, str):
                    if not parameter.default:
                        argname = f"[{argname}]"
                    else:
                        argname += "=" + parameter.default
                argtext += argname
                argtext += " "
            s += f"{self.marker}{command} {argtext}\n    {method.__doc__}\n"
        return s

    def _cmd_help(self):
        """Returns the help listing"""
        self.output(self.get_help_text())

    def _cmd_license(self):
        """Returns the licensing information"""
        mw_license = getattr(CommandProcessor, "license", None)
        if not mw_license:
            with open(Utils.local_path("LICENSE")) as f:
                CommandProcessor.license = mw_license = f.read()
        self.output(mw_license)

    def default(self, raw: str):
        self.output("Echo: " + raw)

    def _error_unknown_command(self, raw: str):
        self.output(f"Could not find command {raw}. Known commands: {', '.join(self.commands)}")

    def _error_parsing_command(self, exception: Exception):
        self.output(str(exception))


class ClientMessageProcessor(CommandProcessor):
    marker = "!"
    ctx: Context

    def __init__(self, ctx: Context, client: Client):
        self.ctx = ctx
        self.client = client

    def output(self, text):
        notify_client(self.client, text)

    def default(self, raw: str):
        pass  # default is client sending just text

    def _cmd_players(self) -> bool:
        """Get information about connected and missing players"""
        if len(self.ctx.player_names) < 10:
            notify_all(self.ctx, get_players_string(self.ctx))
        else:
            self.output(get_players_string(self.ctx))
        return True

    def _cmd_forfeit(self) -> bool:
        """Surrender and send your remaining items out to their recipients"""
        if "enabled" in self.ctx.forfeit_mode:
            forfeit_player(self.ctx, self.client.team, self.client.slot)
            return True
        elif "disabled" in self.ctx.forfeit_mode:
            self.output(
                "Sorry, client forfeiting has been disabled on this server. You can ask the server admin for a /forfeit")
            return False
        else:  # is auto or goal
            if self.ctx.client_game_state[self.client.team, self.client.slot] == CLIENT_GOAL:
                forfeit_player(self.ctx, self.client.team, self.client.slot)
                return True
            else:
                self.output(
                    "Sorry, client forfeiting requires you to have beaten the game on this server."
                    " You can ask the server admin for a /forfeit")
                if self.client.version < [2, 1, 0]:
                    self.output(
                        "Your client is too old to send game beaten information. Please update, load you savegame and reconnect.")
                return False

    def _cmd_remaining(self) -> bool:
        """List remaining items in your game, but not their location or recipient"""
        if self.ctx.remaining_mode == "enabled":
            remaining_item_ids = get_remaining(self.ctx, self.client.team, self.client.slot)
            if remaining_item_ids:
                self.output("Remaining items: " + ", ".join(Items.lookup_id_to_name.get(item_id, "unknown item")
                                                            for item_id in remaining_item_ids))
            else:
                self.output("No remaining items found.")
            return True
        elif self.ctx.remaining_mode == "disabled":
            self.output(
                "Sorry, !remaining has been disabled on this server.")
            return False
        else:  # is goal
            if self.ctx.client_game_state[self.client.team, self.client.slot] == CLIENT_GOAL:
                remaining_item_ids = get_remaining(self.ctx, self.client.team, self.client.slot)
                if remaining_item_ids:
                    self.output("Remaining items: " + ", ".join(Items.lookup_id_to_name.get(item_id, "unknown item")
                                                                for item_id in remaining_item_ids))
                else:
                    self.output("No remaining items found.")
                return True
            else:
                self.output(
                    "Sorry, !remaining requires you to have beaten the game on this server")
                if self.client.version < [2, 1, 0]:
                    self.output(
                        "Your client is too old to send game beaten information. Please update, load you savegame and reconnect.")
                return False

    def _cmd_countdown(self, seconds: str = "10") -> bool:
        """Start a countdown in seconds"""
        try:
            timer = int(seconds, 10)
        except ValueError:
            timer = 10
        asyncio.create_task(countdown(self.ctx, timer))
        return True

    def _cmd_missing(self) -> bool:
        """List all missing location checks from the server's perspective"""
        buffer = ""  # try not to spam small packets over network
        count = 0
        for location_id, location_name in Regions.lookup_id_to_name.items():  # cheat console is -1, keep in mind
            if location_id != -1 and location_id not in self.ctx.location_checks[self.client.team, self.client.slot]:
                buffer += f'Missing: {location_name}\n'
                count += 1

        if buffer:
            self.output(buffer + f"Found {count} missing location checks")
        else:
            self.output("No missing location checks found.")
        return True

    @mark_raw
    def _cmd_alias(self, alias_name: str = ""):
        if alias_name:
            alias_name = alias_name[:15].strip()
            self.ctx.name_aliases[self.client.team, self.client.slot] = alias_name
            self.output(f"Hello, {alias_name}")
            update_aliases(self.ctx, self.client.team)
            save(self.ctx)
            return True
        elif (self.client.team, self.client.slot) in self.ctx.name_aliases:
            del (self.ctx.name_aliases[self.client.team, self.client.slot])
            self.output("Removed Alias")
            update_aliases(self.ctx, self.client.team)
            save(self.ctx)
            return True
        return False

    @mark_raw
    def _cmd_getitem(self, item_name: str) -> bool:
        """Cheat in an item"""
        if self.ctx.item_cheat:
            item_name, usable, response = get_intended_text(item_name, Items.item_table.keys())
            if usable:
                new_item = ReceivedItem(Items.item_table[item_name][3], -1, self.client.slot)
                get_received_items(self.ctx, self.client.team, self.client.slot).append(new_item)
                notify_all(self.ctx, 'Cheat console: sending "' + item_name + '" to ' + self.ctx.get_aliased_name(self.client.team, self.client.slot))
                send_new_items(self.ctx)
                return True
            else:
                self.output(response)
                return False
        else:
            self.output("Cheating is disabled.")
            return False

    @mark_raw
    def _cmd_hint(self, item_or_location: str = "") -> bool:
        """Use !hint {item_name/location_name}, for example !hint Lamp or !hint Link's House. """
        points_available = self.ctx.location_check_points * len(
            self.ctx.location_checks[self.client.team, self.client.slot]) - \
                           self.ctx.hint_cost * self.ctx.hints_used[self.client.team, self.client.slot]
        if not item_or_location:
            self.output(f"A hint costs {self.ctx.hint_cost} points. "
                        f"You have {points_available} points.")
            hints = {hint.re_check(self.ctx, self.client.team) for hint in
                     self.ctx.hints[self.client.team, self.client.slot]}
            self.ctx.hints[self.client.team, self.client.slot] = hints
            notify_hints(self.ctx, self.client.team, list(hints))
            return True
        else:
            item_name, usable, response = get_intended_text(item_or_location)
            if usable:
                if item_name in Items.hint_blacklist:
                    self.output(f"Sorry, \"{item_name}\" is marked as non-hintable.")
                    hints = []
                elif item_name in Items.item_table:  # item name
                    hints = collect_hints(self.ctx, self.client.team, self.client.slot, item_name)
                else:  # location name
                    hints = collect_hints_location(self.ctx, self.client.team, self.client.slot, item_name)

                if hints:
                    new_hints = set(hints) - self.ctx.hints[self.client.team, self.client.slot]
                    old_hints = set(hints) - new_hints
                    if old_hints:
                        notify_hints(self.ctx, self.client.team, list(old_hints))
                        if not new_hints:
                            self.output("Hint was previously used, no points deducted.")
                    if new_hints:
                        found_hints = sum(not hint.found for hint in new_hints)
                        if not found_hints:  # everything's been found, no need to pay
                            can_pay = True
                        elif self.ctx.hint_cost:
                            can_pay = points_available // (self.ctx.hint_cost * found_hints) >= 1
                        else:
                            can_pay = True

                        if can_pay:
                            self.ctx.hints_used[self.client.team, self.client.slot] += found_hints

                            for hint in new_hints:
                                if not hint.found:
                                    self.ctx.hints[self.client.team, hint.finding_player].add(hint)
                                    self.ctx.hints[self.client.team, hint.receiving_player].add(hint)
                            notify_hints(self.ctx, self.client.team, list(new_hints))
                            save(self.ctx)
                        else:
                            self.output(f"You can't afford the hint. "
                                        f"You have {points_available} points and need at least "
                                        f"{self.ctx.hint_cost}, "
                                        f"more if multiple items are still to be found.")
                        return True
                else:
                    self.output("Nothing found. Item/Location may not exist.")
                    return False
            else:
                self.output(response)
                return False


async def process_client_cmd(ctx: Context, client: Client, cmd, args):
    if type(cmd) is not str:
        await send_msgs(client, [['InvalidCmd']])
        return

    if cmd == 'Connect':
        if not args or type(args) is not dict or \
                'password' not in args or type(args['password']) not in [str, type(None)] or \
                'rom' not in args or type(args['rom']) is not list:
            await send_msgs(client, [['InvalidArguments', 'Connect']])
            return

        errors = set()
        if ctx.password is not None and args['password'] != ctx.password:
            errors.add('InvalidPassword')

        if tuple(args['rom']) not in ctx.rom_names:
            errors.add('InvalidRom')
        else:
            team, slot = ctx.rom_names[tuple(args['rom'])]
            if any([c.slot == slot and c.team == team for c in ctx.clients if c.auth]):
                errors.add('SlotAlreadyTaken')
            else:
                client.name = ctx.player_names[(team, slot)]
                client.team = team
                client.slot = slot

        if errors:
            await send_msgs(client, [['ConnectionRefused', list(errors)]])
        else:
            client.auth = True
            client.version = args.get('version', Client.version)
            client.tags = args.get('tags', Client.tags)
            reply = [['Connected', [(client.team, client.slot),
                                    [(p, ctx.get_aliased_name(t, p)) for (t, p), n in ctx.player_names.items() if
                                     t == client.team]]]]
            items = get_received_items(ctx, client.team, client.slot)
            if items:
                reply.append(['ReceivedItems', (0, tuplize_received_items(items))])
                client.send_index = len(items)
            await send_msgs(client, reply)
            await on_client_joined(ctx, client)

    if client.auth:
        if cmd == 'Sync':
            items = get_received_items(ctx, client.team, client.slot)
            if items:
                client.send_index = len(items)
                await send_msgs(client, [['ReceivedItems', (0, tuplize_received_items(items))]])

        elif cmd == 'LocationChecks':
            if type(args) is not list:
                await send_msgs(client, [['InvalidArguments', 'LocationChecks']])
                return
            register_location_checks(ctx, client.team, client.slot, args)

        elif cmd == 'LocationScouts':
            if type(args) is not list:
                await send_msgs(client, [['InvalidArguments', 'LocationScouts']])
                return
            locs = []
            for location in args:
                if type(location) is not int or 0 >= location > len(Regions.location_table):
                    await send_msgs(client, [['InvalidArguments', 'LocationScouts']])
                    return
                loc_name = list(Regions.location_table.keys())[location - 1]
                target_item, target_player = ctx.locations[(Regions.location_table[loc_name][0], client.slot)]

                replacements = {'SmallKey': 0xA2, 'BigKey': 0x9D, 'Compass': 0x8D, 'Map': 0x7D}
                item_type = [i[2] for i in Items.item_table.values() if type(i[3]) is int and i[3] == target_item]
                if item_type:
                    target_item = replacements.get(item_type[0], target_item)

                locs.append([loc_name, location, target_item, target_player])

            # logging.info(f"{client.name} in team {client.team+1} scouted {', '.join([l[0] for l in locs])}")
            await send_msgs(client, [['LocationInfo', [l[1:] for l in locs]]])

        elif cmd == 'UpdateTags':
            if not args or type(args) is not list:
                await send_msgs(client, [['InvalidArguments', 'UpdateTags']])
                return
            client.tags = args

        elif cmd == 'GameFinished':
            if ctx.client_game_state[client.team, client.slot] != CLIENT_GOAL:
                finished_msg = f'{ctx.get_aliased_name(client.team, client.slot)} (Team #{client.team + 1}) has found the triforce.'
                notify_all(ctx, finished_msg)
                ctx.client_game_state[client.team, client.slot] = CLIENT_GOAL
                if "auto" in ctx.forfeit_mode:
                    forfeit_player(ctx, client.team, client.slot)

        if cmd == 'Say':
            if type(args) is not str or not args.isprintable():
                await send_msgs(client, [['InvalidArguments', 'Say']])
                return

            notify_all(ctx, ctx.get_aliased_name(client.team, client.slot) + ': ' + args)
            client.messageprocessor(args)



def set_password(ctx: Context, password):
    ctx.password = password
    logging.warning('Password set to ' + password if password else 'Password disabled')


class ServerCommandProcessor(CommandProcessor):
    ctx: Context

    def __init__(self, ctx: Context):
        self.ctx = ctx
        super(ServerCommandProcessor, self).__init__()

    def default(self, raw: str):
        notify_all(self.ctx, '[Server]: ' + raw)

    @mark_raw
    def _cmd_kick(self, player_name: str) -> bool:
        """Kick specified player from the server"""
        for client in self.ctx.clients:
            if client.auth and client.name.lower() == player_name.lower() and client.socket and not client.socket.closed:
                asyncio.create_task(client.socket.close())
                self.output(f"Kicked {self.ctx.get_aliased_name(client.team, client.slot)}")
                return True

        self.output(f"Could not find player {player_name} to kick")
        return False

    def _cmd_save(self) -> bool:
        """Save current state to multidata"""
        save(self.ctx)
        self.output("Game saved")
        return True

    def _cmd_players(self) -> bool:
        """Get information about connected players"""
        self.output(get_players_string(self.ctx))
        return True

    def _cmd_exit(self) -> bool:
        """Shutdown the server"""
        asyncio.create_task(self.ctx.server.ws_server._close())
        self.ctx.running = False
        return True

    @mark_raw
    def _cmd_alias(self, player_name_then_alias_name):
        player_name, alias_name = player_name_then_alias_name.split(" ", 1)
        player_name, usable, response = get_intended_text(player_name, self.ctx.player_names.values())
        if usable:
            for (team, slot), name in self.ctx.player_names.items():
                if name == player_name:
                    if alias_name:
                        alias_name = alias_name.strip()[:15]
                        self.ctx.name_aliases[team, slot] = alias_name
                        self.output(f"Named {player_name} as {alias_name}")
                        update_aliases(self.ctx, team)
                        save(self.ctx)
                        return True
                    else:
                        del (self.ctx.name_aliases[team, slot])
                        self.output(f"Removed Alias for {player_name}")
                        update_aliases(self.ctx, team)
                        save(self.ctx)
                        return True
        else:
            self.output(response)
            return False

    @mark_raw
    def _cmd_password(self, new_password: str = "") -> bool:
        """Set the server password. Leave the password text empty to remove the password"""
        set_password(self.ctx, new_password if new_password else None)
        return True

    @mark_raw
    def _cmd_forfeit(self, player_name: str) -> bool:
        """Send out the remaining items from a player's game to their intended recipients"""
        seeked_player = player_name.lower()
        for (team, slot), name in self.ctx.player_names.items():
            if name.lower() == seeked_player:
                forfeit_player(self.ctx, team, slot)
                return True

        self.output(f"Could not find player {player_name} to forfeit")
        return False

    def _cmd_send(self, player_name: str, *item_name: str) -> bool:
        """Sends an item to the specified player"""
        seeked_player, usable, response = get_intended_text(player_name, self.ctx.player_names.values())
        if usable:
            item = " ".join(item_name)
            item, usable, response = get_intended_text(item, Items.item_table.keys())
            if usable:
                for client in self.ctx.clients:
                    if client.name == seeked_player:
                        new_item = ReceivedItem(Items.item_table[item][3], -1, client.slot)
                        get_received_items(self.ctx, client.team, client.slot).append(new_item)
                        notify_all(self.ctx, 'Cheat console: sending "' + item + '" to ' + self.ctx.get_aliased_name(client.team, client.slot))
                        send_new_items(self.ctx)
                        return True
            else:
                self.output(response)
                return False
        else:
            self.output(response)
            return False

    def _cmd_hint(self, player_name: str, *item_or_location: str) -> bool:
        """Send out a hint for a player's item or location to their team"""
        seeked_player, usable, response = get_intended_text(player_name, self.ctx.player_names.values())
        if usable:
            for (team, slot), name in self.ctx.player_names.items():
                if name == seeked_player:
                    item = " ".join(item_or_location)
                    item, usable, response = get_intended_text(item)
                    if usable:
                        if item in Items.item_table:  # item name
                            hints = collect_hints(self.ctx, team, slot, item)
                            notify_hints(self.ctx, team, hints)
                        else:  # location name
                            hints = collect_hints_location(self.ctx, team, slot, item)
                            notify_hints(self.ctx, team, hints)
                        return True
                    else:
                        self.output(response)
                        return False

        else:
            self.output(response)
            return False


async def console(ctx: Context):
    session = prompt_toolkit.PromptSession()
    while ctx.running:
        with patch_stdout():
            input_text = await session.prompt_async()
        try:
            ctx.commandprocessor(input_text)
        except:
            import traceback
            traceback.print_exc()


async def forward_port(port: int):
    import upnpy
    import socket

    upnp = upnpy.UPnP()
    upnp.discover()
    device = upnp.get_igd()

    service = device['WANPPPConnection.1']

    # get own lan IP
    ip = socket.gethostbyname(socket.gethostname())

    # This specific action returns an empty dict: {}
    service.AddPortMapping(
        NewRemoteHost='',
        NewExternalPort=port,
        NewProtocol='TCP',
        NewInternalPort=port,
        NewInternalClient=ip,
        NewEnabled=1,
        NewPortMappingDescription='Berserker\'s Multiworld',
        NewLeaseDuration=60 * 60 * 24  # 24 hours
    )

    logging.info(f"Attempted to forward port {port} to {ip}, your local ip address.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    defaults = Utils.get_options()["server_options"]
    parser.add_argument('--host', default=defaults["host"])
    parser.add_argument('--port', default=defaults["port"], type=int)
    parser.add_argument('--password', default=defaults["password"])
    parser.add_argument('--multidata', default=defaults["multidata"])
    parser.add_argument('--savefile', default=defaults["savefile"])
    parser.add_argument('--disable_save', default=defaults["disable_save"], action='store_true')
    parser.add_argument('--loglevel', default=defaults["loglevel"],
                        choices=['debug', 'info', 'warning', 'error', 'critical'])
    parser.add_argument('--location_check_points', default=defaults["location_check_points"], type=int)
    parser.add_argument('--hint_cost', default=defaults["hint_cost"], type=int)
    parser.add_argument('--disable_item_cheat', default=defaults["disable_item_cheat"], action='store_true')
    parser.add_argument('--port_forward', default=defaults["port_forward"], action='store_true')
    parser.add_argument('--forfeit_mode', default=defaults["forfeit_mode"], nargs='?',
                        choices=['auto', 'enabled', 'disabled', "goal", "auto-enabled"], help='''\
                             Select !forfeit Accessibility. (default: %(default)s)
                             auto:     Automatic "forfeit" on goal completion
                             enabled:  !forfeit is always available
                             disabled: !forfeit is never available
                             goal:     !forfeit can be used after goal completion
                             auto-enabled: !forfeit is available and automatically triggered on goal completion
                             ''')
    parser.add_argument('--remaining_mode', default=defaults["remaining_mode"], nargs='?',
                        choices=['enabled', 'disabled', "goal"], help='''\
                             Select !remaining Accessibility. (default: %(default)s)
                             enabled:  !remaining is always available
                             disabled: !remaining is never available
                             goal:     !remaining can be used after goal completion
                             ''')
    args = parser.parse_args()
    return args


async def main(args: argparse.Namespace):
    logging.basicConfig(format='[%(asctime)s] %(message)s', level=getattr(logging, args.loglevel.upper(), logging.INFO))
    portforwardtask = None
    if args.port_forward:
        portforwardtask = asyncio.create_task(forward_port(args.port))

    ctx = Context(args.host, args.port, args.password, args.location_check_points, args.hint_cost,
                  not args.disable_item_cheat, args.forfeit_mode, args.remaining_mode)

    ctx.data_filename = args.multidata

    try:
        if not ctx.data_filename:
            import tkinter
            import tkinter.filedialog
            root = tkinter.Tk()
            root.withdraw()
            ctx.data_filename = tkinter.filedialog.askopenfilename(filetypes=(("Multiworld data","*multidata"),))

        with open(ctx.data_filename, 'rb') as f:
            jsonobj = json.loads(zlib.decompress(f.read()).decode("utf-8-sig"))
            for team, names in enumerate(jsonobj['names']):
                for player, name in enumerate(names, 1):
                    ctx.player_names[(team, player)] = name
            ctx.rom_names = {tuple(rom): (team, slot) for slot, team, rom in jsonobj['roms']}
            ctx.remote_items = set(jsonobj['remote_items'])
            ctx.locations = {tuple(k): tuple(v) for k, v in jsonobj['locations']}
            if "er_hint_data" in jsonobj:
                ctx.er_hint_data = {int(player): {int(address): name for address, name in loc_data.items()}
                                    for player, loc_data in jsonobj["er_hint_data"].items()}
    except Exception as e:
        logging.exception('Failed to read multiworld data (%s)' % e)
        return

    ip = args.host if args.host else Utils.get_public_ipv4()



    ctx.disable_save = args.disable_save
    if not ctx.disable_save:
        if not ctx.save_filename:
            ctx.save_filename = (ctx.data_filename[:-9] if ctx.data_filename[-9:] == 'multidata' else (
                    ctx.data_filename + '_')) + 'multisave'
        try:
            with open(ctx.save_filename, 'rb') as f:
                jsonobj = json.loads(zlib.decompress(f.read()).decode("utf-8"))
                ctx.set_save(jsonobj)
        except FileNotFoundError:
            logging.error('No save data found, starting a new game')
        except Exception as e:
            logging.exception(e)
    if portforwardtask:
        try:
            await portforwardtask
        except:
            logging.exception("Automatic port forwarding failed with:")
    ctx.server = websockets.serve(functools.partial(server, ctx=ctx), ctx.host, ctx.port, ping_timeout=None,
                                  ping_interval=None)
    logging.info('Hosting game at %s:%d (%s)' % (ip, ctx.port,
                                                 'No password' if not ctx.password else 'Password: %s' % ctx.password))
    await ctx.server
    await console(ctx)

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(parse_args()))

#!/usr/bin/python3
import argparse
import asyncio
import functools
import json
import os
import struct
import sys
import time
import uuid

import aiohttp
import spotipy.util

if sys.platform.startswith('win'):
    import psutil


class DiscordRPC:
    def __init__(self):
        if sys.platform in ['linux', 'darwin']:
            env_vars = ['XDG_RUNTIME_DIR', 'TMPDIR', 'TMP', 'TEMP']
            path = next((os.environ.get(path, None)
                         for path in env_vars if path in os.environ), '/tmp')
            self.ipc_path = f'{path}/discord-ipc-0'
            self.loop = asyncio.get_event_loop()

        elif sys.platform.startswith('win'):
            self.ipc_path = r'\\?\pipe\discord-ipc-0'
            self.loop = asyncio.ProactorEventLoop()

        parser = argparse.ArgumentParser()
        parser.add_argument('--verbose', '-v', action='store_true', default=False)
        self.verbose = parser.parse_args().verbose
        self.session = aiohttp.ClientSession(loop=self.loop)
        self.spotify = {
            'id': 'd5e182d9623e4ed98b2aa881a66ffd61',
            'secret': '52ae98e925704aae97d57d958099f490',
            'redirect_uri': 'http://localhost:8080',
            'scope': 'user-read-currently-playing'
        }

        self.username = None
        self.token = None

        self.read_loop = None
        self.sock_reader = None
        self.sock_writer = None

    async def read_output(self):
        while True:
            data = await self.sock_reader.read(1024)
            if data == b'':
                self.sock_writer.close()
                exit(0)

            if self.verbose:
                try:
                    code, length = struct.unpack('<ii', data[:8])
                    print(
                        f'OP Code: {code}; Length: {length}; Response:\n{json.loads(data[8:].decode("utf-8"))}\n')
                except struct.error:
                    print(f'Something happened')
                    print(data)
            await asyncio.sleep(1)

    def send_data(self, op: int, payload: dict):
        payload = json.dumps(payload)
        self.sock_writer.write(struct.pack('<ii', op, len(payload)) + payload.encode('utf-8'))

    async def handshake(self):
        if sys.platform in ['linux', 'darwin']:
            self.sock_reader, self.sock_writer = await asyncio.open_unix_connection(self.ipc_path, loop=self.loop)

        elif sys.platform.startswith('win'):
            self.sock_reader = asyncio.StreamReader(loop=self.loop)
            reader_protocol = asyncio.StreamReaderProtocol(self.sock_reader, loop=self.loop)
            self.sock_writer, _ = await self.loop.create_pipe_connection(lambda: reader_protocol, self.ipc_path)

        self.send_data(0, {'v': 1, 'client_id': '384332129905147904'})

    async def get_spotify_token(self):
        if os.path.isfile(f'.cache-{self.username}'):
            config = json.load(open(f'.cache-{self.username}'))
            if int(config['expires_at'] / 1000) > time.time():
                self.token = config['access_token']
                return

        kwargs = {'scope': self.spotify['scope'], 'client_id': self.spotify['id'],
                  'client_secret': self.spotify['secret'], 'redirect_uri': self.spotify['redirect_uri']}
        func = functools.partial(spotipy.util.prompt_for_user_token, self.username, **kwargs)
        self.token = await self.loop.run_in_executor(None, func)

    @staticmethod
    async def get_pid(process_name: str):
        if sys.platform in ['linux', 'darwin']:
            process = await asyncio.create_subprocess_shell(f'pgrep {process_name}', stdout=asyncio.subprocess.PIPE)
            stdout = (await process.communicate())[0]
            if stdout == b'':
                print('Spotify is not launched')
                exit(1)
            return int(stdout.decode('utf-8').split('\n')[0])

        elif sys.platform.startswith('win'):
            process_list = [p for p in psutil.process_iter() if p.name() == process_name]
            if process_list:
                return next(reversed(process_list)).pid
            print('Spotify is not launched')
            exit(1)

    def get_config(self):
        if not os.path.isfile('config.json'):
            print("Config does not exist. Creating.")
            self.username = input("Enter your username: ")

            data = {'username': self.username}
            with open('config.json', 'w+') as config_file:
                json.dump(data, config_file)
                print('Config file was created successfully!')
        else:
            self.username = json.load(open('config.json'))['username']

    async def detect_now_playing(self):
        timestamp = None
        init = True
        pid = os.getpid()
        if sys.platform == 'linux':
            pid = await self.get_pid('spotify')
        elif sys.platform == 'darwin':
            pid = await self.get_pid('Spotify')
        elif sys.platform.startswith('win'):
            pid = await self.get_pid('Spotify.exe')

        async def run(data: dict, paused=False):
            try:
                async with self.session.request('GET', data['item']['album']['href'], headers={'Authorization': f'Bearer {self.token}'}) as resp:
                    album = json.loads(await resp.text())
                    _time = int(time.time()) - int(data['progress_ms'] / 1000)
                    payload = {
                        'cmd': 'SET_ACTIVITY',
                        'args': {
                            'activity': {
                                'state': f'{album["name"]}' if not paused else f'{album["name"]} [Paused]',
                                'details': f'{data["item"]["name"]} by {", ".join([artist["name"] for artist in data["item"]["artists"]])}',
                                'party': {
                                    'size': [data['item']['track_number'], album['tracks']['total']]
                                },
                                'assets': {
                                    'large_text': album['uri'],
                                    'large_image': 'spotify',
                                    'small_text': data['item']['uri'],
                                    'small_image': 'playing'
                                },
                            },
                            'pid': pid
                        },
                        'nonce': str(uuid.uuid4())
                    }
                    if paused:
                        payload['args']['activity']['assets']['small_image'] = 'paused'
                    if not paused:
                        payload['args']['activity']['timestamps'] = {
                            'start': _time,
                            'end': _time + int(data['item']['duration_ms'] / 1000)
                        }
                    self.send_data(1, payload)
                    return data['timestamp']
            except:
                pass

        while True:
            async with self.session.request('GET', 'https://api.spotify.com/v1/me/player/currently-playing', headers={'Authorization': f'Bearer {self.token}'}) as response:
                text = await response.text()
                try:
                    d = text if text == '' else json.loads(text)
                except ValueError:
                    print(f'Something happened\n{text}')

            if d != '' and d.get('error', None):
                msg = d['error']['message']
                if msg == 'Invalid access token' or msg == 'The access token expired':
                    await self.get_spotify_token()
                else:
                    print(f'Something happened\n{d}')
                    exit(1)

            if not init and d == '':
                _pid = None
                if sys.platform == 'linux':
                    _pid = await self.get_pid('spotify')
                elif sys.platform == 'darwin':
                    _pid = await self.get_pid('Spotify')
                elif sys.platform.startswith('win'):
                    _pid = await self.get_pid('Spotify.exe')
                if not _pid:
                    exit(0)

            if init or (timestamp if 'timestamp' not in d else d['timestamp']) != timestamp:
                if init and d == '' or not d['is_playing'] and d['progress_ms'] == 0:
                    self.send_data(1, {
                        'cmd': 'SET_ACTIVITY',
                        'args': {
                            'activity': {
                                'details': 'Not playing anything...',
                                'assets': {
                                    'large_text': 'Spotify',
                                    'large_image': 'spotify'
                                },
                            },
                            'pid': pid
                        },
                        'nonce': str(uuid.uuid4())
                    })
                    timestamp = time.time() if d == '' else d['timestamp']
                elif not d['is_playing'] and d['progress_ms'] != 0:
                    timestamp = await run(d, paused=True)
                elif d['is_playing']:
                    timestamp = await run(d)
                init = False
            await asyncio.sleep(5)

    async def run(self):
        print("Starting...")
        await self.handshake()
        self.read_loop = self.loop.create_task(self.read_output())
        self.get_config()
        await self.get_spotify_token()
        print("Ready!")
        print("The app is now running! Leave this window open!")
        await self.detect_now_playing()

    def close(self):
        self.read_loop.cancel()
        self.sock_writer.close()
        self.session.close()
        self.loop.close()
        exit(0)


if __name__ == '__main__':
    rpc = DiscordRPC()
    try:
        rpc.loop.run_until_complete(rpc.run())
    except KeyboardInterrupt:
        rpc.close()

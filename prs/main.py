import asyncio
import hashlib
import enum
import json

import pydantic
import pydantic_settings
import uvicorn

from uuid import UUID, uuid4

from fastapi import FastAPI, Header
from starlette.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocket, WebSocketDisconnect

from prs.Entity.entity import Room, Player, PlayerChoice


app = FastAPI()


class HashingSettings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(env_prefix="HASHING_", case_sensitive=False)
    salt: str


class PlayerTokenizer:
    def __init__(self):
        self._settings = HashingSettings()

    def generate_token(self, player: Player) -> str:
        salted_player_id = str(player.id) + self._settings.salt

        return hashlib.sha256(salted_player_id.encode()).hexdigest()


player_tokenizer = PlayerTokenizer()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []  # после : указіваю тип

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket, code: int, reason: str | None = None):
        self.active_connections.remove(websocket)
        await websocket.close(code, reason)

    @staticmethod
    async def send_personal_message(message: str, websocket: WebSocket):
        await websocket.send_text(message)
    # async def broadcast(self, message: str):
    #     for connection in self.active_connections:
    #         await connection.send_text(message)


class RoomsManager: # отвечает за связь комнаты с интерфейсом(апи/методов работы с сервером)
    def __init__(self):
        self.rooms: dict[UUID, Room] = {}  #создаю пустое множество(колекцию) комнат с типом Словарь у которого ключ ююайди == ююайди комнаті
        self.players_and_websocket: dict[UUID, WebSocket] = {}

    def create(self, name: str, req_players: int) -> Room:
        current_room = Room(id=uuid4(), name=name, required_players=req_players)
        self.rooms[current_room.id] = current_room
        return current_room

    def get_room(self, room_id: UUID) -> Room | None:
        return self.rooms.get(room_id, None)    # get принимает два значения 1 получает по первому если нет то что во втором параметре

    def register_player(self, player: Player, websocket: WebSocket) -> None:
        self.players_and_websocket[player.id] = websocket

    def get_websockets_for_room(self, room: Room) -> list[WebSocket]:
        '''Возвращаем вебсокеты всех игроков, которые находятся в комнате'''
        return [self.players_and_websocket[player.id] for player in room.players]


class PlayerManager:
    def __init__(self):
        self._tokenizer = PlayerTokenizer()
        self.hash_with_player: dict[str, Player] = {}
        self.player_websockets: dict[UUID, WebSocket] = {}

    def register_player(self, player: Player, websocket: WebSocket) -> str:
        player_hash = self._tokenizer.generate_token(player)
        self.hash_with_player[player_hash] = player
        self.player_websockets[player.id] = websocket

        return player_hash

    def get_player(self, player_hash: str) -> Player | None:
        return self.hash_with_player.get(player_hash, None)

    def get_player_websocket(self, player: Player) -> WebSocket:
        return self.player_websockets[player.id]


player_manager = PlayerManager()
room_manager = RoomsManager()  #создание обькта класса идет со скобками


@app.post("/create_room")
async def create_room(name: str, req_players: int) -> Room:
    new_room: Room = room_manager.create(name, req_players)
    return new_room


manager = ConnectionManager()


class RoomEvent(enum.Enum):
    ConnectedToRoom = "ConnectedToRoom"
    NewPlayerConnected = "NewPlayerConnected"
    PlayerDisconnected = "PlayerDisconnected"
    GameCanBeStart = "GameCanBeStart"
    Draw = "Draw"
    Win = "Win"
    Lose = "Lose"


class RoomEventMessage(pydantic.BaseModel):
    event: RoomEvent
    room: Room


@app.websocket("/start/{room_id}")
async def websocket_connect_room(websocket: WebSocket, room_id: UUID, name: str, player_hash: str | None = None):
    await manager.connect(websocket)

    try:
        
        room = room_manager.get_room(room_id)
        if not room:
            await manager.disconnect(websocket, 1003, reason="room net")
            return

        other_players_websocket = room_manager.get_websockets_for_room(room)

        if player_hash:
            player = player_manager.get_player(player_hash)

            if not player:
                player = Player(name=name)
                room.add_player(player)
                player_hash = player_manager.register_player(player, websocket)
                room_manager.register_player(player, websocket)

        else:
            player = Player(name=name)
            room.add_player(player)
            player_hash = player_manager.register_player(player, websocket)
            room_manager.register_player(player, websocket)

        #  ПРИМЕР Отправка сообщения на клиент
        await websocket.send_text(json.dumps({
            "event": RoomEvent.ConnectedToRoom.value,
            "room": room.model_dump(mode="json"),
            "hash": player_hash,
        }))

        if not room.can_start:
            for other_player_websocket in other_players_websocket:
                #  ПРИМЕР Отправка сообщения на клиент
                await other_player_websocket.send_text(json.dumps({
                    "event": RoomEvent.NewPlayerConnected.value,
                    "room": room.model_dump(mode="json"),
                }))

        while True:
            if room.can_start:  # когда подключилось заданое количество игроков
                all_players_websocket = room_manager.get_websockets_for_room(room)

                # TODO: Отправка сообщения на клиент
                for i in all_players_websocket:
                    #  ПРИМЕР Отправка сообщения на клиент
                    await i.send_text(json.dumps({
                        "event": RoomEvent.GameCanBeStart.value,
                        "room": room.model_dump(mode="json"),
                    }))

                room = room_manager.get_room(room.id)
                if room.all_players_make_choice:
                    winners = room.winners
                    for room_player in room.players:
                        room_player_websocket = player_manager.get_player_websocket(player)

                        if room_player in winners:
                            if len(winners) == len(room.players):
                                await manager.send_personal_message(json.dumps({
                                    "event": RoomEvent.Draw.value,
                                    "room": room.model_dump(mode="json"),
                                }), room_player_websocket)
                            else:
                                await manager.send_personal_message(json.dumps({
                                    "event": RoomEvent.Win.value,
                                    "room": room.model_dump(mode="json"),
                                }), room_player_websocket)
                        else:
                            await manager.send_personal_message(json.dumps({
                                "event": RoomEvent.Lose.value,
                                "room": room.model_dump(mode="json"),
                            }), room_player_websocket)

                player_input = await websocket.receive_text()
                try:
                    player.choice = PlayerChoice(player_input)
                except ValueError:
                    # TODO: Отправка сообщения на клиент
                    await manager.send_personal_message(f"not valid choice", websocket)
            await asyncio.sleep(0.3)
            # await manager.broadcast(f"Client #{client_id} says: {data}")
    except WebSocketDisconnect:
        await manager.disconnect(websocket, 1003, reason="konec")
        # await manager.broadcast(f"Client #{client_id} left the chat")


# @app.websocket("/ws")
# async def websocket_endpoint(websocket: WebSocket, room_id: UUID | None = None):
#     await manager.connect(websocket)
#     try:
#         # if not room_id:
#         #     new_room = room_manager.create()
#         while True:
#             data = await websocket.receive_text()
#
#             # new_room = room_manager.create()
#             await manager.send_personal_message(f"You wrote: {new_room.id}", websocket)
#             # await manager.broadcast(f"Client #{client_id} says: {data}")
#     except WebSocketDisconnect:
#         manager.disconnect(websocket)
#         # await manager.broadcast(f"Client #{client_id} left the chat")


app.add_middleware(
    CORSMiddleware,
    allow_origins=("http://localhost:5173", "http://localhost:8080"),
    allow_credentials=True,
    allow_methods=("POST", "GET",),
    allow_headers=("*",)
)

if __name__ == '__main__':
    uvicorn.run("prs.main:app", port=7000, log_level='info')

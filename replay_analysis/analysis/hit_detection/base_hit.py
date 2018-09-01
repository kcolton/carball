import math
import time
from typing import List, Dict, Callable
import logging

import numpy as np
import pandas as pd

from ...generated.api import game_pb2
from ...generated.api.stats.events_pb2 import Hit
from ...json_parser.game import Game
from .hitbox.hitbox import Hitbox

COLLISION_DISTANCE_HIGH_LIMIT = 500
COLLISION_DISTANCE_LOW_LIMIT = 250

logger = logging.getLogger(__name__)


class BaseHit:

    @staticmethod
    def get_hits_from_game(game: Game, proto_game: game_pb2, id_creation: Callable,
                           data_frame: pd.DataFrame) -> Dict[int, Hit]:

        start_time = time.time()

        team_dict = {}
        all_hits = {}  # frame_number: [{hit_data}, {hit_data}] for hit guesses
        for team in game.teams:
            team_dict[team.is_orange] = team

        hit_frame_numbers = BaseHit.get_hit_frame_numbers_by_ball_ang_vel(game)

        hit_creation_time = time.time()
        logger.info('time to get get frame_numbers: %s', (hit_creation_time - start_time) * 1000)

        positional_columns = ['pos_x', 'pos_y', 'pos_z', 'rot_x', 'rot_y', 'rot_z']
        hit_frames = data_frame.loc[hit_frame_numbers, (slice(None), positional_columns)]
        player_displacements = {player.name: get_player_ball_displacements(hit_frames, player.name)
                                for player in game.players}
        player_distances = {player_name: get_distance_from_displacements(data_frame).rename(player_name)
                            for player_name, data_frame in player_displacements.items()}

        player_distances_data_frame = pd.concat(player_distances, axis=1)

        rotation_matrices = {player.name: get_rotation_matrices(hit_frames, player.name) for player in game.players}

        local_displacements: Dict[str, pd.DataFrame] = {
            player.name: get_local_displacement(player_displacements[player.name],
                                                rotation_matrices[player.name])
            for player in game.players
        }

        player_hitboxes = get_player_hitboxes(game)
        collision_distances = [
            get_collision_distances(local_displacements[player.name], player_hitboxes[player.name]).rename(player.name)
            for player in game.players
        ]
        collision_distances_data_frame = pd.concat(collision_distances, axis=1)

        # using hit_team_no, loop through players in team to find distances
        for player in game.players:
            create_rotation_matrix(hit_frames, player.name)

        # find closest player in team to ball for known hits
        for frame_number in hit_frame_numbers:
            try:
                team = team_dict[game.ball.loc[frame_number, 'hit_team_no']]
            except KeyError:
                continue
            closest_player = None
            closest_player_distance = 999999
            for player in team.players:
                if len(player.loadout) == 1:
                    player_hitbox = get_hitbox(player.loadout[0]['car'])
                else:
                    player_hitbox = get_hitbox(player.loadout[player.is_orange]['car'])
                try:
                    player_position = player.data.loc[frame_number, ['pos_x', 'pos_y', 'pos_z']]
                    ball_position = game.ball.loc[frame_number, ['pos_x', 'pos_y', 'pos_z']]
                    ball_displacement = ball_position - player_position

                    player_rotation = player.data.loc[frame_number, ['rot_x', 'rot_y', 'rot_z']]
                except KeyError:
                    continue

                joined = pd.concat([player_rotation, ball_displacement])
                joined.dropna(inplace=True)
                if joined.any():
                    ball_displacement = joined.loc[['pos_x', 'pos_y', 'pos_z']].values
                    player_rotation = joined.loc[['rot_x', 'rot_y', 'rot_z']].values
                    ball_unrotated_displacement = unrotate_position(ball_displacement, player_rotation)

                    collision_distance = get_distance(ball_unrotated_displacement, player_hitbox)
                    if collision_distance < closest_player_distance:
                        closest_player = player
                        closest_player_distance = collision_distance

            # TODO: Check if this works with ball_type == 'Basketball'
            # COLLISION_DISTANCE_HIGH_LIMIT probably needs to be increased if Basketball.
            if closest_player_distance < COLLISION_DISTANCE_HIGH_LIMIT:
                hit_player = closest_player
                hit_collision_distance = closest_player_distance
            else:
                hit_player = None
                hit_collision_distance = 999999

            if hit_player is not None:
                hit = proto_game.game_stats.hits.add()
                hit.frame_number = frame_number
                goal_number = data_frame.at[frame_number, ('game', 'goal_number')]
                if not math.isnan(goal_number):
                    hit.goal_number = int(goal_number)
                id_creation(hit.player_id, hit_player.name)
                hit.collision_distance = hit_collision_distance
                hit.ball_data.pos_x = float(ball_position['pos_x'])
                hit.ball_data.pos_y = float(ball_position['pos_y'])
                hit.ball_data.pos_z = float(ball_position['pos_z'])
                all_hits[frame_number] = hit

        time_diff = time.time() - hit_creation_time
        logger.info('ball hit creation time: %s', time_diff * 1000)
        return all_hits

    @staticmethod
    def get_hit_frame_numbers_by_ball_ang_vel(game) -> List[int]:
        ball_ang_vels = game.ball.loc[:, ['ang_vel_x', 'ang_vel_y', 'ang_vel_z']]
        diff_series = ball_ang_vels.diff().any(axis=1)
        indices = diff_series.index[diff_series].tolist()
        return indices

    @staticmethod
    def get_ball_data(game: Game, hit: Hit):
        return game.ball.loc[hit.frame_number, :]


def get_player_ball_displacements(data_frame: pd.DataFrame, player_name: str) -> pd.DataFrame:
    player_df = data_frame[player_name]
    ball_df = data_frame['ball']
    position_column_names = ['pos_x', 'pos_y', 'pos_z']
    return player_df[position_column_names] - ball_df[position_column_names]


def get_distance_from_displacements(data_frame: pd.DataFrame) -> pd.Series:
    position_column_names = ['pos_x', 'pos_y', 'pos_z']
    return np.sqrt((data_frame[position_column_names] ** 2).sum(axis=1))


def get_rotation_matrices(data_frame: pd.DataFrame, player_name: str) -> pd.Series:
    pitch = data_frame[player_name, 'rot_x']
    yaw = data_frame[player_name, 'rot_y']
    roll = data_frame[player_name, 'rot_z']

    cos_roll = np.cos(roll).rename('cos_roll')
    sin_roll = np.sin(roll).rename('sin_roll')
    cos_pitch = np.cos(pitch).rename('cos_pitch')
    sin_pitch = np.sin(pitch).rename('sin_pitch')
    cos_yaw = np.cos(yaw).rename('cos_yaw')
    sin_yaw = np.sin(yaw).rename('sin_yaw')

    components: pd.DataFrame = pd.concat([cos_roll, sin_roll, cos_pitch, sin_pitch, cos_yaw, sin_yaw], axis=1)

    rotation_matrix = components.apply(get_rotation_matrix_from_row, axis=1, result_type='reduce')
    return rotation_matrix


def get_rotation_matrix_from_row(components: pd.Series) -> np.array:
    cos_roll, sin_roll, cos_pitch, sin_pitch, cos_yaw, sin_yaw = components.values
    rotation_matrix = np.array(
        [[cos_pitch * cos_yaw, cos_yaw * sin_pitch * sin_roll - cos_roll * sin_yaw,
          -cos_roll * cos_yaw * sin_pitch - sin_roll * sin_yaw],
         [cos_pitch * sin_yaw, sin_yaw * sin_pitch * sin_roll + cos_roll * cos_yaw,
          -cos_roll * sin_yaw * sin_pitch + sin_roll * cos_yaw],
         [sin_pitch, -cos_pitch * sin_roll, cos_pitch * cos_roll]])
    return rotation_matrix


def get_local_displacement(displacement: pd.DataFrame, rotation_matrices: pd.Series) -> pd.DataFrame:
    position_column_names = ['pos_x', 'pos_y', 'pos_z']
    displacement_vectors = np.expand_dims(displacement[position_column_names].values, 2)
    inverse_rotation_matrices: pd.Series = np.transpose(rotation_matrices)
    inverse_rotation_array = np.stack(inverse_rotation_matrices.values)
    local_displacement = np.matmul(inverse_rotation_array, displacement_vectors)
    displacement_data_frame = pd.DataFrame(data=np.squeeze(local_displacement, 2),
                                           index=displacement.index,
                                           columns=position_column_names)
    return displacement_data_frame


def get_player_hitboxes(game: Game) -> Dict[str, Hitbox]:
    player_hitboxes = {}
    for player in game.players:
        car_item_id = player.loadout[0]['car'] if len(player.loadout) == 1 else player.loadout[player.is_orange]['car']
        player_hitboxes[player.name] = Hitbox(car_item_id)
    return player_hitboxes


def get_collision_distances(local_ball_displacement: pd.DataFrame, player_hitbox: Hitbox) -> pd.Series:
    def get_distance_function_for_player(displacement: pd.Series):
        return player_hitbox.get_collision_distance(displacement.values)

    collision_distances = local_ball_displacement.apply(get_distance_function_for_player, axis=1, result_type='reduce')
    return collision_distances

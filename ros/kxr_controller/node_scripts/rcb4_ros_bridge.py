#!/usr/bin/env python3

import os
import os.path as osp
import shlex
import subprocess
from threading import Lock

import actionlib
from kxr_controller.msg import ServoOnOffAction
from kxr_controller.msg import ServoOnOffResult
import numpy as np
import rospy
from sensor_msgs.msg import JointState
from skrobot.model import RobotModel
from skrobot.utils.urdf import no_mesh_load_mode
import yaml

from rcb4.armh7interface import ARMH7Interface
from rcb4.armh7interface import WormmoduleStruct


np.set_printoptions(precision=0, suppress=True)


def load_yaml(file_path, Loader=yaml.SafeLoader):
    """Load a YAML file into a Python dict.

    Parameters
    ----------
    file_path : str or pathlib.PosixPath
        The path to the YAML file.

    Returns
    -------
    data : dict
        A dict with the loaded yaml data.
    """
    if not osp.exists(str(file_path)):
        raise OSError('{} not exists'.format(str(file_path)))
    with open(osp.expanduser(file_path), 'r') as f:
        data = yaml.load(f, Loader=Loader)
    return data


def run_robot_state_publisher(namespace=None):
    command = f'/opt/ros/{os.environ["ROS_DISTRO"]}/bin/rosrun'
    command += " robot_state_publisher robot_state_publisher"
    if namespace is not None:
        command += f" _tf_prefix:={namespace}"
    command = shlex.split(command)
    process = subprocess.Popen(command)
    return process


def set_fullbody_controller(joint_names):
    controller_yaml_dict = {
        'type': 'position_controllers/JointTrajectoryController',
        'joints': joint_names,
    }
    rospy.set_param('kxr_fullbody_controller', controller_yaml_dict)


def set_joint_state_controler():
    rospy.set_param('joint_state_controller', {
        'type': 'joint_state_controller/JointStateController',
        'publish_rate': 10
    })


def set_robot_description(urdf_path,
                          param_name='robot_description'):
    with open(urdf_path) as f:
        rospy.set_param(param_name, f.read())


class RCB4ROSBridge(object):

    def __init__(self):
        self.lock = Lock()
        r = RobotModel()
        urdf_path = rospy.get_param('~urdf_path')
        with open(urdf_path) as f:
            with no_mesh_load_mode():
                r.load_urdf_file(f)

        servo_config_path = rospy.get_param('~servo_config_path')
        self.joint_name_to_id = load_yaml(servo_config_path)[
            'joint_name_to_servo_id']

        joint_list = [j for j in r.joint_list
                      if j.__class__.__name__ != 'FixedJoint']
        self.joint_names = [j.name for j in joint_list]

        full_namespace = rospy.get_namespace()
        last_slash_pos = full_namespace.rfind('/')
        clean_namespace = full_namespace[:last_slash_pos] \
            if last_slash_pos != 0 else ''

        set_robot_description(
            urdf_path,
            param_name=clean_namespace + '/robot_description')
        set_fullbody_controller(self.joint_names)
        set_joint_state_controler()
        self.current_joint_states_pub = rospy.Publisher(
            clean_namespace + '/current_joint_states',
            JointState,
            queue_size=1)

        self.arm = ARMH7Interface()
        arm = self.arm
        arm.auto_open()
        arm.search_servo_ids()
        arm.search_worm_ids()
        arm.search_wheel_sids()
        arm.all_jointbase_sensors()
        arm.send_stretch(40)

        self.id_to_index = self.arm.servo_id_to_index()
        self.servo_id_to_worm_id = self.arm.servo_id_to_worm_id

        self.joint_servo_on = {jn: False for jn in self.joint_names}
        servo_on_states = arm.servo_states()
        for jn in self.joint_names:
            idx = self.joint_name_to_id[jn]
            if idx in servo_on_states:
                self.joint_servo_on[jn] = True
            else:
                self.joint_servo_on[jn] = False
        print(self.joint_servo_on)
        print(self.id_to_index)

        self.worm_servo_ids = [
            arm.memory_cstruct(WormmoduleStruct, idx).servo_id
            for idx in arm.worm_sorted_ids]

        self.command_joint_state_sub = rospy.Subscriber(
            clean_namespace + '/command_joint_state',
            JointState, queue_size=1,
            callback=self.command_joint_state_callback)

        self.servo_on_off_server = actionlib.SimpleActionServer(
            clean_namespace
            + '/kxr_fullbody_controller/servo_on_off_real_interface',
            ServoOnOffAction,
            execute_cb=self.servo_on_off_callback,
            auto_start=False)
        self.servo_on_off_server.start()

        self.proc_controller_spawner = subprocess.Popen(
            [f'/opt/ros/{os.environ["ROS_DISTRO"]}/bin/rosrun',
             'controller_manager', 'spawner']
            + ['joint_state_controller', 'kxr_fullbody_controller'])
        self.proc_robot_state_publisher = run_robot_state_publisher(
            clean_namespace)

    def command_joint_state_callback(self, msg):
        servo_ids = []
        av_length = len(self.arm.servo_sorted_ids)
        svs = np.zeros(av_length)
        valid_indices = []
        worm_av = []
        worm_indices = []
        for name, angle in zip(msg.name, msg.position):
            if name not in self.joint_name_to_id:
                continue
            if name in self.joint_servo_on \
               and self.joint_servo_on[name] is False:
                continue
            idx = self.joint_name_to_id[name]

            # should ignore duplicated index.
            if idx not in self.id_to_index:
                continue
            if self.id_to_index[idx] in valid_indices:
                continue

            angle = np.rad2deg(angle)
            if idx in self.worm_servo_ids:
                worm_idx = self.servo_id_to_worm_id[idx]
                worm_av.append(angle)
                worm_indices.append(worm_idx)
                angle = 135
            svs[self.id_to_index[idx]] = angle
            valid_indices.append(self.id_to_index[idx])
            servo_ids.append(idx)
        tmp_av = np.ones(av_length + 1)
        if len(valid_indices) == 0:
            return
        try:
            tmp_av[:len(svs)] = np.array(svs)
        except Exception:
            return
        svs = np.matmul(self.arm.joint_to_actuator_matrix, tmp_av)[
            np.array(valid_indices)]
        indices = np.argsort(servo_ids)
        svs = svs[indices]
        servo_ids = np.array(servo_ids)[indices]
        with self.lock:
            if len(worm_indices) > 0:
                worm_av_tmp = np.array(self.arm.read_cstruct_slot_vector(
                    WormmoduleStruct, 'ref_angle'), dtype=np.float32)
                worm_av_tmp[np.array(worm_indices)] = np.array(worm_av)
                self.arm.write_cstruct_slot_v(
                    WormmoduleStruct, 'ref_angle', worm_av_tmp)
            self.arm.servo_angle_vector(servo_ids, svs, velocity=0)

    def servo_on_off_callback(self, goal):
        servo_vector = []
        servo_ids = []
        for joint_name, servo_on in zip(
                goal.joint_names, goal.servo_on_states):
            if joint_name not in self.joint_name_to_id:
                continue
            servo_ids.append(self.joint_name_to_id[joint_name])
            if servo_on:
                servo_vector.append(32767)
                self.joint_servo_on[joint_name] = True
            else:
                servo_vector.append(32768)
                self.joint_servo_on[joint_name] = False
        servo_vector = np.array(servo_vector)
        indices = np.argsort(servo_ids)
        servo_vector = servo_vector[indices]
        servo_ids = np.array(servo_ids)[indices]
        with self.lock:
            self.arm.servo_angle_vector(servo_ids, servo_vector, velocity=10)
        return self.servo_on_off_server.set_succeeded(ServoOnOffResult())

    def run(self):
        rate = rospy.Rate(100)

        while not rospy.is_shutdown():
            with self.lock:
                av = self.arm.angle_vector()
            msg = JointState()
            msg.header.stamp = rospy.Time.now()
            for name in self.joint_names:
                if name in self.joint_name_to_id:
                    servo_id = self.joint_name_to_id[name]
                    if servo_id in self.id_to_index:
                        msg.position.append(
                            np.deg2rad(
                                av[self.id_to_index[servo_id]]))
                        msg.name.append(name)
            self.current_joint_states_pub.publish(msg)
            rate.sleep()


if __name__ == '__main__':
    rospy.init_node('rcb4_ros_bridge')
    ros_bridge = RCB4ROSBridge()  # NOQA
    ros_bridge.run()

import json
import os
import math
import glob
import numpy as np
import png
import shutil

import bpy
from mathutils import Euler, Matrix, Vector

from src.utility.BlenderUtility import get_all_mesh_objects, load_image
from src.utility.Utility import Utility
from src.writer.StateWriter import StateWriter


def load_json(path, keys_to_int=False):
    """Loads content of a JSON file.
    From the BOP toolkit (https://github.com/thodan/bop_toolkit).

    :param path: Path to the JSON file.
    :return: Content of the loaded JSON file.
    """
    # Keys to integers.
    def convert_keys_to_int(x):
        return {int(k) if k.lstrip('-').isdigit() else k: v for k, v in x.items()}

    with open(path, 'r') as f:
        if keys_to_int:
            content = json.load(f, object_hook=lambda x: convert_keys_to_int(x))
        else:
            content = json.load(f)

    return content


def save_json(path, content):
    """ Saves the content to a JSON file in a human-friendly format.
    From the BOP toolkit (https://github.com/thodan/bop_toolkit).

    :param path: Path to the output JSON file.
    :param content: Dictionary/list to save.
    """
    with open(path, 'w') as f:

        if isinstance(content, dict):
            f.write('{\n')
            content_sorted = sorted(content.items(), key=lambda x: x[0])
            for elem_id, (k, v) in enumerate(content_sorted):
                f.write(
                    '  \"{}\": {}'.format(k, json.dumps(v, sort_keys=True)))
                if elem_id != len(content) - 1:
                    f.write(',')
                f.write('\n')
            f.write('}')

        elif isinstance(content, list):
            f.write('[\n')
            for elem_id, elem in enumerate(content):
                f.write('  {}'.format(json.dumps(elem, sort_keys=True)))
                if elem_id != len(content) - 1:
                    f.write(',')
                f.write('\n')
            f.write(']')

        else:
            json.dump(content, f, sort_keys=True)


def save_depth(path, im):
    """Saves a depth image (16-bit) to a PNG file.
    From the BOP toolkit (https://github.com/thodan/bop_toolkit).

    :param path: Path to the output depth image file.
    :param im: ndarray with the depth image to save.
    """
    if not path.endswith(".png"):
        raise ValueError('Only PNG format is currently supported.')

    im[im > 65535] = 65535
    im_uint16 = np.round(im).astype(np.uint16)

    # PyPNG library can save 16-bit PNG and is faster than imageio.imwrite().
    w_depth = png.Writer(im.shape[1], im.shape[0], greyscale=True, bitdepth=16)
    with open(path, 'wb') as f:
        w_depth.write(f, np.reshape(im_uint16, (-1, im.shape[1])))


class BopWriter(StateWriter):
    """ Saves the synthesized dataset in the BOP format. The dataset is split
        into chunks which are saved as individual "scenes". For more details
        about the BOP format, visit the BOP toolkit docs:
        https://github.com/thodan/bop_toolkit/blob/master/docs/bop_datasets_format.md

    **Attributes per object**:

    .. csv-table::
        :header: "Keyword", "Description"

        "dataset", "Annotations for objects of this dataset will be saved. Type: string."
        "append_to_existing_output", "If true, the new frames will be appended to the existing ones. "
                                    "Type: bool. Default: False"
        "postprocessing_modules", "A dict of list of postprocessing modules. The key in the dict specifies the output "
                            "to which the postprocessing modules should be applied. Every postprocessing module "
                            "has to have a run function which takes in the raw data and returns the processed "
                            "data. Type: dict."
    """

    def __init__(self, config):
        StateWriter.__init__(self, config)

        # Parse configuration.
        self.dataset = self.config.get_string("dataset")

        self.append_to_existing_output = self.config.get_bool("append_to_existing_output", False)

        self.postprocessing_modules_per_output = {}
        module_configs = config.get_raw_dict("postprocessing_modules", {})
        for output_key in module_configs:
            self.postprocessing_modules_per_output[output_key] = Utility.initialize_modules(module_configs[output_key])

        # Number of frames saved in each chunk.
        self.frames_per_chunk = 1000

        # Multiply the output depth image with this factor to get depth in mm.
        self.depth_scale = 0.1

        # Format of the depth images.
        depth_ext = '.png'

        # Output paths.
        base_path = self._determine_output_dir(False)
        self.dataset_dir = os.path.join(base_path, 'bop_data', self.dataset)
        self.chunks_dir = os.path.join(self.dataset_dir, 'train_synt')
        self.camera_path = os.path.join(self.dataset_dir, 'camera.json')
        self.rgb_tpath = os.path.join(
            self.chunks_dir, '{chunk_id:06d}', 'rgb', '{im_id:06d}' + '{im_type}')
        self.depth_tpath = os.path.join(
            self.chunks_dir, '{chunk_id:06d}', 'depth', '{im_id:06d}' + depth_ext)
        self.chunk_camera_tpath = os.path.join(
            self.chunks_dir, '{chunk_id:06d}', 'scene_camera.json')
        self.chunk_gt_tpath = os.path.join(
            self.chunks_dir, '{chunk_id:06d}', 'scene_gt.json')

        # Create the output directory structure.
        if not os.path.exists(self.dataset_dir):
            os.makedirs(self.dataset_dir)
            os.makedirs(self.chunks_dir)
        elif not self.append_to_existing_output:
            raise Exception("The output folder already exists: {}.".format(
                self.dataset_dir))

    def run(self):
        """ Stores frames and annotations for objects from the specified dataset.
        """
        # Select objects from the specified dataset.
        all_mesh_objects = get_all_mesh_objects()
        self.dataset_objects = []
        for obj in all_mesh_objects:
            if "bop_dataset_name" in obj:
                if obj["bop_dataset_name"] == self.dataset:
                    self.dataset_objects.append(obj)

        # Check if there is any object from the specified dataset.
        if not self.dataset_objects:
            raise Exception("The scene does not contain any object from the "
                            "specified dataset: {}".format(self.dataset))

        # Get the camera.
        cam_ob = bpy.context.scene.camera
        self.cam = cam_ob.data
        self.cam_pose = (self.cam, cam_ob)

        # Save the data.
        self._write_camera()
        self._write_frames()

    def _apply_postprocessing(self, output_key, data):
        """ Applies all postprocessing modules registered for this output type.

        :param output_key: The key of the output type. Type: string.
        :param data: The numpy data.
        :return: The modified numpy data after doing the postprocessing
        """
        if output_key in self.postprocessing_modules_per_output:
            for module in self.postprocessing_modules_per_output[output_key]:
                data = module.run(data)

        return data

    def _load_and_postprocess(self, file_path, key):
        """
        Loads an image and post process it.
        :param file_path: Image path. Type: string.
        :param key: The image's key with regards to the hdf5 file. Type: string.
        :return: The post-processed image that was loaded using the file path.
        """        
        
        data = load_image(Utility.resolve_path(file_path))

        data = self._apply_postprocessing(key, data)

        print("Key: " + key + " - shape: " + str(data.shape) + " - dtype: " + str(
            data.dtype) + " - path: " + file_path)

        return data

    def _get_camera_attribute(self, cam_pose, attribute_name):
        """ Returns the value of the requested attribute for the given object.

        :param cam_pose: camera pose
        :param attribute_name: The attribute name.
        :return: The attribute value.
        """
        cam, cam_ob = cam_pose

        if attribute_name == "fov_x":
            return cam.angle_x
        elif attribute_name == "fov_y":
            return cam.angle_y
        elif attribute_name == "shift_x":
            return cam.shift_x
        elif attribute_name == "shift_y":
            return cam.shift_y
        elif attribute_name == "half_fov_x":
            return cam.angle_x * 0.5
        elif attribute_name == "half_fov_y":
            return cam.angle_y * 0.5
        elif attribute_name == 'loaded_intrinsics':
            return cam['loaded_intrinsics']

        return super()._get_attribute(cam_ob, attribute_name)

    def _get_object_attribute(self, object, attribute_name):
        """ Returns the value of the requested attribute for the given object.

        :param object: The mesh object.
        :param attribute_name: The attribute name.
        :return: The attribute value.
        """
        if attribute_name == "id":
            return object["category_id"]

        return super()._get_attribute(object, attribute_name)

    def get_camK_from_blender_attributes(self, cam_pose):
        """ Constructs the camera matrix K.

        :param cam_pose: Camera info.
        :return: camera matrix K as 9x1 list.
        """
        shift_x = self._get_camera_attribute(cam_pose, 'shift_x')
        shift_y = self._get_camera_attribute(cam_pose, 'shift_y')
        syn_cam_K = self._get_camera_attribute(cam_pose, 'loaded_intrinsics')

        width = bpy.context.scene.render.resolution_x
        height = bpy.context.scene.render.resolution_y

        cam_K = [0.] * 9
        cam_K[-1] = 1

        max_resolution = max(width, height)
        
        cam_K[0] = syn_cam_K[0]
        cam_K[4] = syn_cam_K[4]
 
        cam_K[2] = width/2. - shift_x * max_resolution
        cam_K[5] = height/2. + shift_y * max_resolution

        return cam_K

    def _write_camera(self):
        """ Writes camera.json into dataset_dir.
        """

        width = bpy.context.scene.render.resolution_x
        height = bpy.context.scene.render.resolution_y

        cam_K = self.get_camK_from_blender_attributes(self.cam_pose)
        camera = {'cx': cam_K[2],
                  'cy': cam_K[5],
                  'depth_scale': self.depth_scale,
                  'fx': cam_K[0],
                  'fy': cam_K[4],
                  'height': height,
                  'width': width}

        save_json(self.camera_path, camera)

        return

    def _get_frame_gt(self):
        """ Returns GT annotations for the active camera.

        :return: A list of GT annotations.
        """
        camera_rotation = self._get_camera_attribute(self.cam_pose, 'rotation_euler')
        camera_translation = self._get_camera_attribute(self.cam_pose, 'location')
        H_c2w = Matrix.Translation(Vector(camera_translation)) @ Euler(
            camera_rotation, 'XYZ').to_matrix().to_4x4()

        # Blender to opencv coordinates.
        H_c2w_opencv = H_c2w @ Matrix.Rotation(math.radians(-180), 4, "X")

        frame_gt = []
        for idx, obj in enumerate(self.dataset_objects):
            object_rotation = self._get_object_attribute(obj, 'rotation_euler')
            object_translation = self._get_object_attribute(obj, 'location')
            H_m2w = Matrix.Translation(Vector(object_translation)) @ Euler(
                object_rotation, 'XYZ').to_matrix().to_4x4()

            cam_H_m2c = (H_m2w.inverted() @ H_c2w_opencv).inverted()

            cam_R_m2c = cam_H_m2c.to_quaternion().to_matrix()
            cam_R_m2c = list(cam_R_m2c[0]) + list(cam_R_m2c[1]) + list(cam_R_m2c[2])
            cam_t_m2c = list(cam_H_m2c.to_translation() * 1000.)

            frame_gt.append({
                'cam_R_m2c': cam_R_m2c,
                'cam_t_m2c': cam_t_m2c,
                'obj_id': self._get_object_attribute(obj, 'id')
            })

        return frame_gt

    def _get_frame_camera(self):
        """ Returns camera parameters for the active camera.
        """
        return {
            'cam_K': list(self.get_camK_from_blender_attributes(self.cam_pose)),
            'depth_scale': self.depth_scale
        }

    def _write_frames(self):
        """ Writes images, GT annotations and camera info.
        """
        # Paths to the already existing chunk folders (such folders may exist
        # when appending to an existing dataset).
        chunk_dirs = sorted(glob.glob(os.path.join(self.chunks_dir, '*')))
        chunk_dirs = [d for d in chunk_dirs if os.path.isdir(d)]

        # Get ID's of the last already existing chunk and frame.
        curr_chunk_id = 0
        curr_frame_id = 0
        if len(chunk_dirs):
            last_chunk_dir = sorted(chunk_dirs)[-1]
            last_chunk_gt_fpath = os.path.join(last_chunk_dir, 'scene_gt.json')
            chunk_gt = load_json(last_chunk_gt_fpath, keys_to_int=True)

            # Last chunk and frame ID's.
            last_chunk_id = int(os.path.basename(last_chunk_dir))
            last_frame_id = int(sorted(chunk_gt.keys())[-1])

            # Current chunk and frame ID's.
            curr_chunk_id = last_chunk_id
            curr_frame_id = last_frame_id + 1
            if curr_frame_id % self.frames_per_chunk == 0:
                curr_chunk_id += 1
                curr_frame_id = 0

        # Initialize structures for the GT annotations and camera info.
        chunk_gt = {}
        chunk_camera = {}
        if curr_frame_id != 0:
            # Load GT and camera info of the chunk we are appending to.
            chunk_gt = load_json(
                self.chunk_gt_tpath.format(chunk_id=curr_chunk_id), keys_to_int=True)
            chunk_camera = load_json(
                self.chunk_camera_tpath.format(chunk_id=curr_chunk_id), keys_to_int=True)

        # Go through all frames.
        num_new_frames = bpy.context.scene.frame_end - bpy.context.scene.frame_start
        for frame_id in range(bpy.context.scene.frame_start, bpy.context.scene.frame_end):
            # Activate frame.
            bpy.context.scene.frame_set(frame_id)

            # Reset data structures and prepare folders for a new chunk.
            if curr_frame_id == 0:
                chunk_gt = {}
                chunk_camera = {}
                os.makedirs(os.path.dirname(
                    self.rgb_tpath.format(chunk_id=curr_chunk_id, im_id=0)))
                os.makedirs(os.path.dirname(
                    self.depth_tpath.format(chunk_id=curr_chunk_id, im_id=0)))

            # Get GT annotations and camera info for the current frame.
            chunk_gt[curr_frame_id] = self._get_frame_gt()
            chunk_camera[curr_frame_id] = self._get_frame_camera()

            # Copy the resulting RGB image.
            rgb_output = self._find_registered_output_by_key("colors")
            if rgb_output is None:
                raise Exception("RGB image has not been rendered.")
            image_type = '.png' if rgb_output['path'].endswith('png') else '.jpg'
            rgb_fpath = self.rgb_tpath.format(chunk_id=curr_chunk_id, im_id=curr_frame_id, im_type=image_type)
            shutil.copyfile(rgb_output['path'] % frame_id, rgb_fpath)

            # Load the resulting depth image.
            depth_output = self._find_registered_output_by_key("depth")
            if depth_output is None:
                raise Exception("Depth image has not been rendered.")
            depth = self._load_and_postprocess(depth_output['path'] % frame_id, "depth")

            # Scale the depth to retain a higher precision (the depth is saved
            # as a 16-bit PNG image with range 0-65535).
            depth_mm = 1000.0 * depth  # [m] -> [mm]
            depth_mm_scaled = depth_mm / float(self.depth_scale)

            # Save the scaled depth image.
            depth_fpath = self.depth_tpath.format(chunk_id=curr_chunk_id, im_id=curr_frame_id)
            save_depth(depth_fpath, depth_mm_scaled)

            # Save the chunk info if we are at the end of a chunk or at the last new frame.
            if ((curr_frame_id + 1) % self.frames_per_chunk == 0) or\
                  (frame_id == num_new_frames - 1):

                # Save GT annotations.
                save_json(self.chunk_gt_tpath.format(chunk_id=curr_chunk_id), chunk_gt)

                # Save camera info.
                save_json(self.chunk_camera_tpath.format(chunk_id=curr_chunk_id), chunk_camera)

                # Update ID's.
                curr_chunk_id += 1
                curr_frame_id = 0
            else:
                curr_frame_id += 1

        return

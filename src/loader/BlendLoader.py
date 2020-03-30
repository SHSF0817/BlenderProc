import bpy
import os
import re

from src.loader.Loader import Loader
from src.utility.Utility import Utility


class BlendLoader(Loader):
    """ Loads entities from a specified .blend file's section/datablock.

        Example:

            {
              "module": "loader.BlendLoader",
              "config": {
                "path": "/path/file.blend",     <--------- path to a .blend file
                "load_from": "objects",         <--------- folder name/ID: `/Collection`, `/Object`, `/Material`, etc.
                "entities": ".*abc.*"           <--------- regular expression, load everything in the dict if not given
              }
            }

    **Configuration**:

    .. csv-table::
       :header: "Parameter", "Description"

       "path", "Path to a .blend file. Type: string."
       "load_from", "Name of the datablock/folder inside .blend file. Always start with '/' Type: string. See known_datablock_names for supported folder names/type IDs."
       "entities", "Regular expression representing a name pattern of entities' names. Optional. Type: string."
    """

    def __init__(self, config):
        Loader.__init__(self, config)
        # supported pairs of {ID type/section names: datablock parameter names}
        self.known_datablock_names = {"/Camera": "cameras",
                                      "/Collection": "collections",
                                      "/Image": "images",
                                      "/Light": "lights",
                                      "/Material": "materials",
                                      "/Mesh": "meshes",
                                      "/Object": "objects",
                                      "/Texture": "textures"}

    def run(self):
        # get a path to a .blend file
        path = Utility.resolve_path(self.config.get_string("path"))
        # get section name/Blend ID
        load_from = self.config.get_string("load_from")
        # get a entities' name regex if present, set to None if not
        if self.config.has_param("entities"):
            entities = self.config.get_string("entities")
        else:
            entities = None

        with bpy.data.libraries.load(path) as (data_from, data_to):
            # check if defined ID is supported
            if load_from in self.known_datablock_names.keys():
                # if some regex was specified, get corresponding matching entity's names
                if entities is not None:
                    entities_to_load = [item for item in getattr(data_from, self.known_datablock_names[load_from])
                                        if re.fullmatch(entities, item) is not None]
                # get all entity's names if not
                else:
                    entities_to_load = getattr(data_from, self.known_datablock_names[load_from])
                # load entities
                for entity_to_load in entities_to_load:
                    bpy.ops.wm.append(filepath=os.path.join(path, load_from, entity_to_load),
                                      filename=entity_to_load,
                                      directory=os.path.join(path + load_from))
            else:
                raise Exception("Unsupported datablock/folder name: " + load_from +
                                "\nSupported names: " + str(self.known_datablock_names.keys()) +
                                "\nIf your ID exists, but not supported, please append a new pair of "
                                "{type ID(folder name): parameter name} to the 'known_datablock_names' dict. Use this "
                                "for finding your parameter name: " + str(dir(data_from)))

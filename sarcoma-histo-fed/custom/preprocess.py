"""
    File name: 0b_tileLoop_deepzoom.py
    Date created: March/2017

    Source:
    Tiling code taken from: https://github.com/ncoudray/DeepPATH
    which in turn was inspired from
    https://github.com/openslide/openslide-python/blob/master/examples/deepzoom/deepzoom_tile.py
    which is Copyright (c) 2010-2015 Carnegie Mellon University
    The code has been extensively modified 

    Objective:
    Tile svs, jpg or dcm images with the possibility of rejecting some tiles based based on xml or jpg masks

    Be careful:
    Overload of the node - may have memory issue if node is shared with other jobs.
"""

import logging
import os
import random
import re
import shutil
import sys
import traceback
from glob import glob
from multiprocessing import JoinableQueue, Process, Queue
from unicodedata import normalize

import cv2 as cv
import numpy as np
import openslide
import spams
from imageio import imread, imwrite
from openslide import ImageSlide, open_slide
from openslide.deepzoom import DeepZoomGenerator
from PIL import Image
from tqdm import tqdm

VIEWER_SLIDE_NAME = "slide"

logger = logging.getLogger(__name__)


class TileWorker(Process):
    """A child process that generates and writes tiles."""

    def __init__(
        self,
        queue,
        slidepath,
        tile_size,
        overlap,
        limit_bounds,
        quality,
        _Bkg,
        _ROIpc,
        baseimage,
        out_queue,
    ):
        Process.__init__(self, name="TileWorker")
        self.logger = logging.getLogger(self.__class__.__name__)
        self.daemon = True
        self._queue = queue
        self._slidepath = slidepath
        self._tile_size = tile_size
        self._overlap = overlap
        self._limit_bounds = limit_bounds
        self._quality = quality
        self._slide = None
        self._Bkg = _Bkg
        self._ROIpc = _ROIpc
        self._baseimage = baseimage
        self._out_queue = out_queue

    def _stain_dict_Vahadane(self, img, thresh=0.8, vlambda=0.10):
        imgLab = cv.cvtColor(img, cv.COLOR_RGB2LAB)
        mask = (imgLab[:, :, 0] / 255.0) < thresh
        if np.sum(mask == True) == 0:
            mask = (imgLab[:, :, 0] / 255.0) < (thresh + 0.1)
            if np.sum(mask == True) == 0:
                mask = (imgLab[:, :, 0] / 255.0) < 1000
        mask = mask.reshape((-1,))
        # RGB to OD
        imgOD = img
        imgOD[(img == 0)] = 1
        imgOD = (-1) * np.log(imgOD / 255)
        imgOD = imgOD.reshape((-1, 3))
        # mask OD
        imgOD = imgOD[mask]
        WisHisHisv = spams.trainDL(
            imgOD.T,
            K=2,
            lambda1=vlambda,
            mode=2,
            modeD=0,
            posAlpha=True,
            posD=True,
            verbose=False,
            numThreads=1,
        ).T
        if WisHisHisv[0, 0] < WisHisHisv[1, 0]:
            WisHisHisv = WisHisHisv[[1, 0], :]
        # normalize rows
        # disregard an empty or black portion in second array of arrays
        if not np.array_equal(WisHisHisv[1], [0, 0, 0]):
            WisHisHisv = WisHisHisv / np.linalg.norm(WisHisHisv, axis=1)[:, None]
        return WisHisHisv

    def _write_normalized_image(self, pil_image, filepath, WisHisHisv, quality):
        descr = """
        Apply Vahadane's normalization on list of images. Reference:
        % @inproceedings{Vahadane2015ISBI,
        %       Author = {Abhishek Vahadane and Tingying Peng and Shadi Albarqouni and Maximilian Baust and Katja Steiger and Anna Melissa Schlitter and Amit Sethi and Irene Esposito and Nassir Navab},
        %       Booktitle = {IEEE International Symposium on Biomedical Imaging},
        %       Title = {Structure-Preserved Color Normalization for Histological Images},
        %       Year = {2015}}

        """
        tile = np.array(pil_image)
        p = np.percentile(tile, 90)
        if p == 0:
            p = 1.0
        img2t = np.clip(tile * 255.0 / p, 0, 255).astype(np.uint8)
        WisHisHisv2 = self._stain_dict_Vahadane(img2t)
        # get concentration
        imgOD2 = img2t
        imgOD2[(img2t == 0)] = 1
        imgOD2 = (-1) * np.log(imgOD2 / 255.0)
        imgOD2 = imgOD2.reshape((-1, 3))
        start_values = (
            spams.lasso(imgOD2.T, D=WisHisHisv2.T, mode=2, lambda1=0.01, pos=True, numThreads=1)
            .toarray()
            .T
        )
        img_end = (255 * np.exp(-1 * np.dot(start_values, WisHisHisv).reshape(tile.shape))).astype(
            np.uint8
        )
        imgout = Image.fromarray(img_end)
        imgout.save(filepath, quality=quality)

    def run(self):
        self._slide = open_slide(self._slidepath)
        last_associated = None
        dz = self._get_dz()

        # Obtain normalized tile to be used for all others
        tile = cv.imread(self._baseimage)
        tile = cv.cvtColor(tile, cv.COLOR_BGR2RGB)
        # standardize brightness
        p = np.percentile(tile, 90)
        tile = np.clip(tile * 255.0 / p, 0, 255).astype(np.uint8)
        # get stain dictionnary
        WisHisHisv = self._stain_dict_Vahadane(tile)

        while True:
            data = self._queue.get()
            if data is None:
                self._queue.task_done()
                break
            # associated, level, address, outfile = data
            (
                associated,
                level,
                address,
                outfile,
                format,
                outfile_bw,
                PercentMasked,
                SaveMasks,
                TileMask,
            ) = data
            if last_associated != associated:
                dz = self._get_dz(associated)
                last_associated = associated
            # try:
            if True:
                try:
                    tile = dz.get_tile(level, address)
                    # A single tile is being read
                    # check the percentage of the image with "information". Should be above 50%
                    gray = tile.convert("L")
                    bw = gray.point(lambda x: 0 if x < 220 else 1, "F")
                    arr = np.array(np.asarray(bw))
                    avgBkg = np.average(bw)
                    bw = gray.point(lambda x: 0 if x < 220 else 1, "1")
                    # ARTHUR ADD
                    # image = Image.open(sys.argv[1])
                    # convert image to numpy array
                    data = np.asarray(gray)
                    np.reshape(data, (-1, 1))
                    u, count_unique = np.unique(data, return_counts=True)
                    # if count_unique.size < 100 then empty tile
                    # std_dev=np.std(data)
                    # if std_dev < 30 then likely an empty tile
                    # print(std_dev)
                    if count_unique.size > 10 and avgBkg <= (self._Bkg / 100.0):
                        # ARTHUR ADD END
                        # check if the image is mostly background
                        # if avgBkg <= (self._Bkg / 100.0):
                        # if an Aperio selection was made, check if is within the selected region
                        if PercentMasked >= (self._ROIpc / 100.0):
                            # if PercentMasked > 0.05:
                            # print("saving " + outfile)
                            try:
                                self._write_normalized_image(
                                    tile, outfile, WisHisHisv, self._quality
                                )
                                self._out_queue.put(outfile)
                            except Warning:
                                self.logger.warn("Skipping " + outfile)
                                continue
                            # print(str(self.out_queue))
                            # print(str(self.out_queue.qsize()))
                            if bool(SaveMasks) == True:
                                height = TileMask.shape[0]
                                width = TileMask.shape[1]
                                TileMaskO = np.zeros((height, width, 3), "uint8")
                                maxVal = float(TileMask.max())
                                TileMaskO[..., 0] = (
                                    TileMask[:, :].astype(float) / maxVal * 255.0
                                ).astype(int)
                                TileMaskO[..., 1] = (
                                    TileMask[:, :].astype(float) / maxVal * 255.0
                                ).astype(int)
                                TileMaskO[..., 2] = (
                                    TileMask[:, :].astype(float) / maxVal * 255.0
                                ).astype(int)
                                TileMaskO = np.array(
                                    Image.fromarray(arr).resize(
                                        TileMaskO, (arr.shape[0], arr.shape[1], 3)
                                    )
                                )
                                TileMaskO[TileMaskO < 10] = 0
                                TileMaskO[TileMaskO >= 10] = 255
                                imwrite(
                                    outfile_bw, TileMaskO
                                )  # (outfile_bw, quality=self._quality)

                        # print("%s good: %f" %(outfile, avgBkg))
                    # elif level>5:
                    #    tile.save(outfile, quality=self._quality)
                    # print("%s empty: %f" %(outfile, avgBkg))
                except:
                    self.logger.warn(level, address)
                    self.logger.warn(
                        "image %s failed at dz.get_tile for level %f" % (self._slidepath, level)
                    )
                finally:
                    self._queue.task_done()

    def _get_dz(self, associated=None):
        if associated is not None:
            image = ImageSlide(self._slide.associated_images[associated])
        else:
            image = self._slide
        return DeepZoomGenerator(
            image, self._tile_size, self._overlap, limit_bounds=self._limit_bounds
        )


class DeepZoomImageTiler(object):
    """Handles generation of tiles and metadata for a single image."""

    def __init__(
        self,
        dz,
        basename,
        format,
        associated,
        queue,
        slide,
        basenameJPG,
        xmlfile,
        mask_type,
        xmlLabel,
        ROIpc,
        ImgExtension,
        SaveMasks,
        Mag,
        out_queue,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._dz = dz
        self._basename = basename
        self._basenameJPG = basenameJPG
        self._format = format
        self._associated = associated
        self._queue = queue
        self._processed = 0
        self._slide = slide
        self._xmlfile = xmlfile
        self._mask_type = mask_type
        self._xmlLabel = xmlLabel
        self._ROIpc = ROIpc
        self._ImgExtension = ImgExtension
        self._SaveMasks = SaveMasks
        self._Mag = Mag
        self.out_queue = out_queue

    def run(self):
        self._write_tiles()
        # self._write_dzi()

    def _write_tiles(self):
        ########################################3
        # nc_added
        # level = self._dz.level_count-1
        Magnification = 20
        tol = 2
        # get slide dimensions, zoom levels, and objective information
        Factors = self._slide.level_downsamples
        try:
            Objective = float(self._slide.properties[openslide.PROPERTY_NAME_OBJECTIVE_POWER])
            # print(self._basename + " - Obj information found")
        except:
            # print(self._basename + " - No Obj information found")
            # print(self._ImgExtension)
            if ("jpg" in self._ImgExtension) | ("dcm" in self._ImgExtension):
                # Objective = self._ROIpc
                Objective = 1.0
                Magnification = Objective
            # print("input is jpg - will be tiled as such with %f" % Objective)
            elif ("tiff" in self._ImgExtension) | ("btf" in self._ImgExtension):
                Objective = 20.0
                Magnification = 20.0
                # print(
                #    "input is tif - will be tiled as with Obj %f and Mag %f"
                #    % (Objective, Magnification)
                # )
            else:
                return
        # calculate magnifications
        Available = tuple(Objective / x for x in Factors)
        # find highest magnification greater than or equal to 'Desired'
        Mismatch = tuple(x - Magnification for x in Available)
        AbsMismatch = tuple(abs(x) for x in Mismatch)
        if len(AbsMismatch) < 1:
            # print(self._basename + " - Objective field empty!")
            return
        """
        if(min(AbsMismatch) <= tol):
            Level = int(AbsMismatch.index(min(AbsMismatch)))
            Factor = 1
        else: #pick next highest level, downsample
            Level = int(max([i for (i, val) in enumerate(Mismatch) if val > 0]))
            Factor = Magnification / Available[Level]
        # end added
        """
        xml_valid = False
        # a dir was provided for xml files

        if True:
            # if self._xmlfile != '' && :
            # print(self._xmlfile, self._ImgExtension)
            ImgID = os.path.basename(self._basename)
            xmldir = os.path.join(self._xmlfile, ImgID + ".xml")
            # print("xml:")
            # print(xmldir)

            for level in range(self._dz.level_count - 1, -1, -1):
                ThisMag = Available[0] / pow(2, self._dz.level_count - (level + 1))
                if self._Mag > 0:
                    if ThisMag != self._Mag:
                        continue
                ########################################
                # tiledir = os.path.join("%s_files" % self._basename, str(level))
                """
                MTL changing output behavior to not put tiled jpegs under slide dir
                instead we put directly under class/label dir, which keras can then suck up
                tiledir = os.path.join("%s_files" % self._basename, str(ThisMag))
                if not os.path.exists(tiledir):
                    os.makedirs(tiledir)
                """
                cols, rows = self._dz.level_tiles[level]
                for row in range(rows):
                    for col in range(cols):
                        InsertBaseName = False
                        # MTL removing the os.path.join that used to make these jpeg tiles under a dir named after slide
                        if InsertBaseName:
                            tilename = "%s_%s_%d_%d.%s" % (
                                self._basename,
                                self._basenameJPG,
                                col,
                                row,
                                self._format,
                            )
                            tilename_bw = "%s_%s_%d_%d_mask.%s" % (
                                self._basename,
                                self._basenameJPG,
                                col,
                                row,
                                self._format,
                            )
                        else:
                            tilename = "%s_%d_%d.%s" % (self._basename, col, row, self._format)
                            tilename_bw = "%s_%d_%d_mask.%s" % (
                                self._basename,
                                col,
                                row,
                                self._format,
                            )
                        PercentMasked = 1.0
                        TileMask = []

                        if not os.path.exists(tilename):
                            self._queue.put(
                                (
                                    self._associated,
                                    level,
                                    (col, row),
                                    tilename,
                                    self._format,
                                    tilename_bw,
                                    PercentMasked,
                                    self._SaveMasks,
                                    TileMask,
                                )
                            )
                        else:
                            self.out_queue.put(tilename)
                        self._tile_done()

    def _tile_done(self):
        self._processed += 1
        count, total = self._processed, self._dz.tile_count
        if count % 100 == 0 or count == total:
            self.logger.debug(
                "Tiling %s: wrote %d/%d tiles" % (self._associated or "slide", count, total)
            )

    def _write_dzi(self):
        with open("%s.dzi" % self._basename, "w") as fh:
            fh.write(self.get_dzi())

    def get_dzi(self):
        return self._dz.get_dzi(self._format)

    def jpg_mask_read(self, xmldir):
        # Original size of the image
        ImgMaxSizeX_orig = float(self._dz.level_dimensions[-1][0])
        ImgMaxSizeY_orig = float(self._dz.level_dimensions[-1][1])
        # Number of centers at the highest resolution
        cols, rows = self._dz.level_tiles[-1]
        # Img_Fact = int(ImgMaxSizeX_orig / 1.0 / cols)
        Img_Fact = 1
        try:
            # xmldir: change extension from xml to *jpg
            xmldir = xmldir[:-4] + "mask.jpg"
            # xmlcontent = read xmldir image
            xmlcontent = imread(xmldir)
            xmlcontent = xmlcontent - np.min(xmlcontent)
            mask = xmlcontent / np.max(xmlcontent)
            # we want image between 0 and 1
            xml_valid = True
        except:
            xml_valid = False
            self.logger.warn("error with minidom.parse(xmldir)")
            return [], xml_valid, 1.0

        return mask, xml_valid, Img_Fact


class DeepZoomStaticTiler(object):
    """Handles generation of tiles and metadata for all images in a slide."""

    def __init__(
        self,
        slidepath,
        basename,
        format,
        tile_size,
        overlap,
        limit_bounds,
        quality,
        workers,
        with_viewer,
        Bkg,
        basenameJPG,
        xmlfile,
        mask_type,
        ROIpc,
        oLabel,
        ImgExtension,
        SaveMasks,
        Mag,
        out_queue,
        baseimage,
    ):
        self._slide = open_slide(slidepath)
        self._basename = basename
        self._basenameJPG = basenameJPG
        self._xmlfile = xmlfile
        self._mask_type = mask_type
        self._format = format
        self._tile_size = tile_size
        self._overlap = overlap
        self._limit_bounds = limit_bounds
        self._queue = JoinableQueue(2 * workers)
        self._workers = workers
        self._with_viewer = with_viewer
        self._Bkg = Bkg
        self._ROIpc = ROIpc
        self._dzi_data = {}
        self._xmlLabel = oLabel
        self._ImgExtension = ImgExtension
        self._SaveMasks = SaveMasks
        self._Mag = Mag
        self.out_queue = out_queue
        self._baseimage = baseimage

        for _i in range(workers):
            TileWorker(
                self._queue,
                slidepath,
                tile_size,
                overlap,
                limit_bounds,
                quality,
                self._Bkg,
                self._ROIpc,
                self._baseimage,
                self.out_queue,
            ).start()

    def run(self):
        self._run_image()
        if self._with_viewer:
            for name in self._slide.associated_images:
                self._run_image(name)
        self._shutdown()

    def _run_image(self, associated=None):
        """Run a single image from self._slide."""
        if associated is None:
            image = self._slide
            if self._with_viewer:
                basename = os.path.join(self._basename, VIEWER_SLIDE_NAME)
            else:
                basename = self._basename
        else:
            image = ImageSlide(self._slide.associated_images[associated])
            basename = os.path.join(self._basename, self._slugify(associated))
        dz = DeepZoomGenerator(
            image, self._tile_size, self._overlap, limit_bounds=self._limit_bounds
        )
        tiler = DeepZoomImageTiler(
            dz,
            basename,
            self._format,
            associated,
            self._queue,
            self._slide,
            self._basenameJPG,
            self._xmlfile,
            self._mask_type,
            self._xmlLabel,
            self._ROIpc,
            self._ImgExtension,
            self._SaveMasks,
            self._Mag,
            self.out_queue,
        )
        tiler.run()
        self._dzi_data[self._url_for(associated)] = tiler.get_dzi()

    def _url_for(self, associated):
        if associated is None:
            base = VIEWER_SLIDE_NAME
        else:
            base = self._slugify(associated)
        return "%s.dzi" % base

    def _copydir(self, src, dest):
        if not os.path.exists(dest):
            os.makedirs(dest)
        for name in os.listdir(src):
            srcpath = os.path.join(src, name)
            if os.path.isfile(srcpath):
                shutil.copy(srcpath, os.path.join(dest, name))

    @classmethod
    def _slugify(cls, text):
        text = normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
        return re.sub("[^a-z0-9]+", "_", text)

    def _shutdown(self):
        for _i in range(self._workers):
            self._queue.put(None)
        self._queue.join()
        self.out_queue.put(None)


def get_sample_label(sample: str, sample_labels: dict) -> str:
    for label in sample_labels.keys():
        if sample in sample_labels[label]:
            return label

    raise RuntimeError("Could not find label for sample: " + sample)


def get_train_valid_split(files: list, sample_labels: dict, validation_split: float):
    file_labels = {}
    for file in files:
        basenameJPG = os.path.splitext(os.path.basename(file))[0]
        sample_class = get_sample_label(basenameJPG, sample_labels)
        file_labels.setdefault(sample_class, []).append(file)

    train_files = []
    validation_files = []
    # ensure each class has appropriate train/valid split
    # otherwise might see skewed representation when classes are not equally represented
    for label in file_labels.keys():
        num_val = int(len(file_labels[label]) * validation_split)
        num_train = len(file_labels[label]) - num_val
        random.shuffle(file_labels[label])
        train_files.extend(file_labels[label][:num_train])
        validation_files.extend(file_labels[label][-num_val:])

    return train_files, validation_files


def get_labelled_tiles(
    slide_list: list,
    output_dir: str,
    sample_labels: dict,
    labels_map: dict,
    tile_size: int,
    overlap: int,
    quality: int,
    workers: int,
    background: float,
    img_extension: str,
    magnification: float,
    baseimage: str,
):
    # using nested dict instead of list here
    # specifically we'll store dict[dict[list[<paths>]]]
    # outermost dict is labels, then dict of slides, and each of those is a list of tiles
    # should be more efficient to do this, than to store a list of
    #   (tilename, label, slidename)
    # which incurs a lot of bloat/repetition for label and slidename
    tile_dict = {v: {} for _, v in labels_map.items()}
    worker_queue = Queue()
    for filename in tqdm(slide_list):
        basenameJPG = os.path.splitext(os.path.basename(filename))[0]
        logger.debug("processing: " + basenameJPG + " with extension: " + img_extension)

        sample_class = get_sample_label(basenameJPG, sample_labels)
        output = os.path.join(output_dir, sample_class, basenameJPG)
        try:
            # if True:
            DeepZoomStaticTiler(
                filename,
                output,
                "jpeg",  # format
                tile_size,
                overlap,
                True,  # limit_bounds
                quality,
                workers,
                False,  # with_viewer
                background,
                basenameJPG,
                "",  # xml file
                1,  # mask_type
                0,  # ROIpc
                "",  # o label
                img_extension,
                False,  # savemasks
                magnification,
                worker_queue,
                baseimage,
            ).run()
            tile_path = worker_queue.get()
            tile_list = tile_dict[labels_map[sample_class]].setdefault(basenameJPG, [])
            while tile_path:
                tile_list.append(tile_path)
                tile_path = worker_queue.get()
        except Exception as e:
            stacktrace = "".join(traceback.TracebackException.from_exception(e).format())
            logger.warn("Failed to process file %s, error: %s" % (filename, stacktrace))

    return tile_dict


def slides_to_tiles(
    slidepath: str,
    overlap: int,
    workers: int,
    augment_tiles: bool,
    output_base: str,
    quality: int,
    tile_size: int,
    background: float,
    magnification: float,
    label_file: str,
    labels_map: dict,
    validation_split: float,
    baseimage: str,
):
    # dict of {label: [list_of_samples_with_label]}
    sample_labels = {}
    with open(label_file, "r") as labelfile:
        for line in labelfile:
            sample, label = line.split(maxsplit=1)
            label = label.strip()
            if label not in labels_map:
                raise RuntimeError(
                    f"{sample} has unknown label {label}. Label map is: {labels_map}"
                )
            sample_labels.setdefault(label.strip(), []).append(sample)

    for l, _ in labels_map.items():
        label_dir = os.path.join(output_base, l)
        os.makedirs(label_dir, exist_ok=True)
    # get  images from the data/ file.
    files = glob(slidepath)
    # ImgExtension = os.path.splitext(slidepath)[1]
    ImgExtension = slidepath.split("*")[-1]
    logger.debug(files)
    train_slides, validation_slides = get_train_valid_split(files, sample_labels, validation_split)

    logger.info("Creating training tiles")
    train_tiles = get_labelled_tiles(
        train_slides,
        output_base,
        sample_labels,
        labels_map,
        tile_size,
        overlap,
        quality,
        workers,
        background,
        ImgExtension,
        magnification,
        baseimage,
    )

    # create a dictionary of the class with the total number of tiles per
    # class
    # class_by_tile_count_dict contains a dict - for example
    # {0: 483, 1: 1120, 2: 197}
    # Class 0 has 483 tiles, Class 1 has 1120 tiles...

    smallest_num_tiles = sys.maxsize
    class_by_tile_count_dict = dict()
    for label, slides in train_tiles.items():
        num_tiles = sum([len(tiles) for _, tiles in slides.items()])
        class_by_tile_count_dict[label] = num_tiles
        if num_tiles < smallest_num_tiles:
            smallest_num_tiles = num_tiles

    new_class_by_tile_count_dict = calc_augmentation_factor(
        class_by_tile_count_dict, smallest_num_tiles
    )

    if augment_tiles:
        # augment tiles and add directly to train_tiles
        logger.info("Augmenting Tiles")
        augment_tiles_based_on_factor(train_tiles, new_class_by_tile_count_dict)

    logger.info("Creating validation tiles")
    validation_tiles = get_labelled_tiles(
        validation_slides,
        output_base,
        sample_labels,
        labels_map,
        tile_size,
        overlap,
        quality,
        workers,
        background,
        ImgExtension,
        magnification,
        baseimage,
    )

    return train_tiles, validation_tiles


# This function will return a similar
# dict to the input but with an augmentation/
# mutliplication factor. The multiplication factor
# is used to normalize the number of tiles across
# classes
#
# For example, the result:
# {0: [512, 8], 1: [1883, 2], 2: [484, 8]}
# where Class 0 has 512 tiles and an augmentation factor of 8
#
# Pass in dict as such
# {0: 483, 1: 1120, 2: 197}
# where 0, 1, and 2 are the class enums
# and the numbers in the value are the total
# number of tiles for each class
#
def calc_augmentation_factor(class_by_tile_count_dict, smallest_num_tiles):
    new_class_by_tile_count_dict = {}

    # loop through the dictionary to set the augmentation_factor
    for key, value in class_by_tile_count_dict.items():
        if value == smallest_num_tiles:
            new_class_by_tile_count_dict[key] = [value, 8]  # 8 is the max augmentation factor
        else:
            # calculate the augmentation factor
            # TODO issue with round function in python3
            augmentation_factor = round(smallest_num_tiles * 8 / value)
            new_class_by_tile_count_dict[key] = [value, augmentation_factor]

    return new_class_by_tile_count_dict


# This function augments the original tile based
# on the augmentation factor and will rotate and mirror
# the image (and save it) based on the augmentation factor
def augment_tiles_based_on_factor(tiles_dict, class_by_tile_count_dict):
    for label, slides in tiles_dict.items():
        for _slide, tiles_list in slides.items():
            new_list = []
            for file_name in tiles_list:
                img = Image.open(file_name)
                augmentation_factor = class_by_tile_count_dict[label][1]

                for i in range(1, augmentation_factor):
                    rotate = (90 * i) % 360
                    # build new file name
                    idx = file_name.rfind(".")
                    mirror = ""
                    if i > 3:
                        mirror = "_mirror"
                    augment_file_name = (
                        file_name[:idx] + "_" + str(rotate) + mirror + file_name[idx:]
                    )

                    # skip the image processing if the augmented file already exists
                    if not os.path.exists(augment_file_name):
                        new_img = rotate_and_mirror_tile(img, rotate, i > 3)
                        # Save the new image
                        new_img.save(augment_file_name)

                    # append to original list
                    new_list.append(augment_file_name)

            tiles_list.extend(new_list)
    return


def rotate_and_mirror_tile(img, degrees, mirror=False):

    new_image = img.rotate(degrees, expand=True)

    if mirror:
        new_image = new_image.transpose(Image.FLIP_LEFT_RIGHT)

    return new_image

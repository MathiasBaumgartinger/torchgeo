# Code for loading dataset licensed under the MIT License.
#
# FLAIR dataset is realeasd under open license 2.0
# ..... https://www.etalab.gouv.fr/wp-content/uploads/2018/11/open-licence.pdf
# ..... https://ignf.github.io/FLAIR/#FLAIR2
#


"""
FLAIR2 dataset.

TODO: add description
The FLAIR2 dataset is a dataset for semantic segmentation of aerial images. It contains aerial images, sentinel-2 images and masks for 13 classes. 
The dataset is split into a training and test set.
"""

import glob
import json
import os
from collections.abc import Callable, Sequence
from typing import ClassVar, Tuple

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import torch
from matplotlib.figure import Figure
from torch import Tensor

from .errors import DatasetNotFoundError, RGBBandsMissingError
from .geo import NonGeoDataset
from .utils import Path, download_url, extract_archive
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch


class FLAIR2(NonGeoDataset):
    splits: ClassVar[Sequence[str]] = ('train', 'test')
    
    url_prefix: ClassVar[str] = "https://storage.gra.cloud.ovh.net/v1/AUTH_366279ce616242ebb14161b7991a8461/defi-ia/flair_data_2"
    # TODO: add checksums for safety
    md5s: dict[str, str] = ""
    
    dir_names: dict[dict[str, str]] = {
        "train": {
            "images": "flair_aerial_train",
            "sentinels": "flair_sen_train",
            "masks": 'flair_labels_train',
        },
        "test": {
            "images": "flair_2_aerial_test",
            "sentinels": "flair_2_sen_test",
            "masks": 'flair_2_labels_test',
        }
    }
    globs: dict[str, str] = {
        "images": "IMG_*.tif",
        "sentinels": {
            "data": "SEN2_*_data.npy",
            "snow_cloud_mask": "SEN2_*_masks.npy"
        },
        "masks": "MSK_*.tif",
    }
    centroids_file: str = "flair-2_centroids_sp_to_patch"
    super_patch_size: int = 40

    # Band information
    rgb_bands: tuple = ("B01", "B02", "B03")
    all_bands: tuple = ("B01", "B02", "B03", "B04", "B05")

    # Note: the original dataset contains 18 classes, but the dataset paper suggests 
    # grouping all classes >13 into "other" class, due to underrepresentation
    classes: tuple[str] = (
        "building",
        "pervious surface",
        "impervious surface",
        "bare soil",
        "water",
        "coniferous",
        "deciduous",
        "brushwood",
        "vineyard",
        "herbaceous vegetation",
        "agricultural land",
        "plowed land",
        "other"
    )

    statistics: dict = {
        "train":{
            "B01": {
                "min": 0.0,
                "max": 255.0,
                "mean": 113.77526983072,
                "stdv": 1.4678962001526,
            },
            "B02": {
                "min": 0.0,
                "max": 255.0,
                "mean": 118.08112962721,
                "stdv": 1.2889349378677,
            },
            "B03": {
                "min": 0.0,
                "max": 255.0,
                "mean": 109.27393364381,
                "stdv": 1.2674219560871,
            },
            "B04": {
                "min": 0.0,
                "max": 255.0,
                "mean": 102.36417944851,
                "stdv": 1.1057592647291,
            },
            "B05": {
                "min": 0.0,
                "max": 255.0,
                "mean": 16.697295721745,
                "stdv": 0.82764953440507,
            },
        }
    }

    @staticmethod
    def per_band_statistics(split: str, bands: Sequence[str] = all_bands) -> tuple[list[float]]:
        """Get statistics (min, max, means, stdvs) for each used band in order.

        Args:
            split (str): Split for which to get statistics (currently only for train)
            bands (Sequence[str], optional): Bands of interest, will be returned in ordered manner. Defaults to all_bands.

        Returns:
            tuple[list[float]]: Filtered, ordered statistics for each band
        """
        assert split in FLAIR2.statistics.keys(), f"Statistics for '{split}' not available; use: '{list(FLAIR2.statistics.keys())}'"
        ordered_bands_statistics = FLAIR2.statistics[split]
        ordered_bands_statistics = list(dict(filter(lambda keyval: keyval[0] in bands, ordered_bands_statistics.items())).values())
        mins = list(map(lambda dict: dict["min"], ordered_bands_statistics))
        maxs = list(map(lambda dict: dict["max"], ordered_bands_statistics))
        means = list(map(lambda dict: dict["mean"], ordered_bands_statistics))
        stdvs = list(map(lambda dict: dict["stdv"], ordered_bands_statistics))
        return mins, maxs, means, stdvs
    
    def __init__(
        self,
        root: Path = 'data',
        split: str = 'train',
        bands: Sequence[str] = all_bands,
        transforms: Callable[[dict[str, Tensor]], dict[str, Tensor]] | None = None,
        download: bool = False,
        checksum: bool = False,
        use_toy: bool = False) -> None:
        """Initialize a new FLAIR2 dataset instance.

        Args:
            root: root directory where dataset can be found
            split: which split to load, one of 'train' or 'test'
            bands: which bands to load (B01, B02, B03, B04, B05)
            transforms: optional transforms to apply to sample
            download: whether to download the dataset if it is not found
            checksum: whether to verify the dataset using checksums
            use_toy: whether to use the a small subset (toy) dataset. CAUTION: should only be used for testing purposes
            
        Raises:
            DatasetNotFoundError
        """
        assert split in self.splits, f"Split '{split}' not in supported splits: '{self.splits}'"

        self.root = root
        self.split = split
        self.transforms = transforms
        self.download = download
        self.checksum = checksum
        self.bands = bands
        self.use_toy = use_toy

        self._verify()
        self.centroids = self._load_centroids(self.centroids_file)

        self.files = self._load_files()
    
    def get_num_bands(self) -> int:
        """Return the number of bands in the dataset.

        Returns:
            int: number of bands in the initialized dataset (might vary from all_bands)
        """
        return len(self.bands)

    def __getitem__(self, index: int) -> dict[str, Tensor]:
        """Return an index within the dataset.

        Args:
            index: index to return

        Returns:
            image and mask at that index with image of dimension `get_num_bands()`x512x512,
            sentinel image of dimension 13x512x512 TODO: verify
            and mask of dimension 512x512
        """
        aerial_fn = self.files[index]["image"]
        sentinel_fn = self.files[index]["sentinel"]
        mask_fn = self.files[index]["mask"]

        aerial = self._load_image(aerial_fn)
        sentinel = self._load_sentinel(sentinel_fn, aerial_fn)
        mask = self._load_target(mask_fn)

        image = aerial 
        sample = {'image': image, "sentinel": sentinel, 'mask': mask}

        if self.transforms is not None:
            sample = self.transforms(sample)

        return sample

    def __len__(self) -> int:
        """Return the number of datapoints in the dataset.

        Returns:
            length of dataset
        """
        return len(self.files)

    def _load_centroids(self, filename: str) -> dict:
        """Load centroids for mapping sentinel super-areas to aerial patches in `flair-2_centroids_sp_to_patch.json`.
        For detailed information on super-patches, see p.4f of datapaper.

        CAUTION: centroids for some reason are stored as y, x

        Args:
            filename: name of the file containing centroids

        Returns:
            dict: centroids for super-patches
        """
        with open(os.path.join(self.root, f"{filename}.json"), "r") as f:
            return json.load(f)
    
    def _crop_super_patch(self, data: np.ndarray, centroid: Tuple[int, int]) -> Tuple[np.ndarray, Tuple[slice, slice]]:
        """Return indices to crop a super-patch from sentinel data based on the centroid coordinates in `flair-2_centroids_sp_to_patch.json`.
        For detailed information on super-patches, see p.4f of datapaper.
        
        Args:
            data: data to crop from
            centroid: centroid coordinates
        
        Returns:
            Tuple[np.ndarray, Tuple[slice, slice]]: original data cropped super-area and the indices used for cropping
        """
        y, x = centroid
        eigth_size = self.super_patch_size // 8
        quarter_size = self.super_patch_size // 4

        indices = (slice(x-eigth_size, x+quarter_size), slice(y-eigth_size, y+quarter_size))
        return data, indices
    
    def _load_files(self) -> list[dict[str, str]]:
        # TODO: add loading of sentinel-2 files
        """Return the paths of the files in the dataset.

        Args:
            root: root dir of dataset

        Returns:
            list of dicts containing paths for each pair of image, masks
        """
        images = sorted(glob.glob(os.path.join(
            self.root, 
            self.dir_names[self.split]["images"],
            "**", self.globs["images"]), recursive=True))
        
        sentinels_data = sorted(glob.glob(os.path.join(
            self.root, 
            self.dir_names[self.split]["sentinels"],
            "**", self.globs["sentinels"]["data"]), recursive=True))
        sentinels_mask = sorted(glob.glob(os.path.join(
            self.root, 
            self.dir_names[self.split]["sentinels"],
            "**", self.globs["sentinels"]["snow_cloud_mask"]), recursive=True))
        sentinels = [
            {"data": data, "snow_cloud_mask": mask}
            for data, mask in zip(sentinels_data, sentinels_mask)
        ]
        
        masks = sorted(glob.glob(os.path.join(
            self.root, 
            self.dir_names[self.split]["masks"],
            "**", self.globs["masks"]), recursive=True))
        
        files = [
            dict(image=image, sentinel=sentinel, mask=mask)
            for image, sentinel, mask in zip(images, sentinels, masks)
        ]
        
        return files

    def _load_image(self, path: Path) -> Tensor:
        # TODO: add loading of sentinel-2 images if requested
        """Load a single image.

        Args:
            path: path to the image

        Returns:
            Tensor: the loaded image
        """
        with rasterio.open(path) as f:
            array: np.typing.NDArray[np.int_] = f.read()
            tensor = torch.from_numpy(array).float()
            # TODO: handle storage optimized format for height data
            if "B05" in self.bands:
                # Height channel will always be the last dimension
                tensor[-1] = torch.div(tensor[-1], 5)
            
        return tensor

    def _load_sentinel(self, paths: list[Path], aerial_path: Path) -> Sequence[Tensor]:
        """Load a sentinel array.

        Args:
            path (Path): path to sentinel directory

        Returns:
            Sequence[Tensor]: ground truth and snow cloud mask as tensors of shape TxCxHxW (time, channels, height, width)
        """
        data = torch.from_numpy(np.load(paths["data"])).float()
        snow_cloud_mask = torch.from_numpy(np.load(paths["snow_cloud_mask"])).float()
        
        img_id = os.path.basename(aerial_path)
        cropped_data, cropping_indices = self._crop_super_patch(data, self.centroids[img_id])
        cropped_snow_cloud_mask, _ = self._crop_super_patch(snow_cloud_mask, self.centroids[img_id])
        
        return [cropped_data, cropped_snow_cloud_mask], cropping_indices
        
    def _load_target(self, path: Path) -> Tensor:
        """Load a single mask corresponding to image.

        Args:
            path: path to the mask

        Returns:
            Tensor: the mask of the image
        """
        with rasterio.open(path) as f:
            array: np.typing.NDArray[np.int_] = f.read(1)
            tensor = torch.from_numpy(array).long()
            # TODO: check if rescaling is smart (i.e. datapaper explains differently -> confusion?)
            # According to datapaper, the dataset contains classes beyond 13
            # however, those are grouped into a single "other" class
            # Rescale the classes to be in the range [0, 12] by subtracting 1
            torch.clamp(tensor - 1, 0, len(self.classes) - 1, out=tensor)
            #torch.clamp(tensor, 0, len(self.classes), out=tensor)
            
        return tensor

    def _verify(self) -> None:
        # TODO: download metadata and check checksums
        """Verify the integrity of the dataset."""
        
        # Change urls/paths/content/configs to toy dataset if requested
        if self.use_toy:
            self._verify_toy()
            self.dir_names["train"] = {k: f.replace("flair", "flair_2_toy") for k, f in self.dir_names["train"].items()}
            self.dir_names["test"] = {k: f.replace("flair_2", "flair_2_toy") for k, f in self.dir_names["test"].items()}
            return
        
        # Check if centroids metadata file or zip is present
        if not os.path.isfile(os.path.join(self.root, f"{self.centroids_file}.json")):
            if not os.path.isfile(os.path.join(self.root, f"{self.centroids_file}.zip")):
                if not self.download:
                    raise DatasetNotFoundError(self)
                self._download(self.centroids_file)
            self._extract(self.centroids_file)
        
        # Files to be extracted
        to_extract: list = []

        # Check if dataset files (by checking glob) are present already
        for train_or_test, dir_name in self.dir_names[self.split].items(): 
            downloaded_path = os.path.join(self.root, dir_name)
            if not os.path.isdir(downloaded_path):
                to_extract.append(dir_name)
                continue

            files_glob = os.path.join(downloaded_path, "**", self.globs[train_or_test])
            if not glob.glob(files_glob, recursive=True):
                to_extract.append(dir_name)
        
        if not to_extract:
            print("Data has been downloaded and extracted already...") 
            return

        # Deepcopy files to be extracted and check wether the zip is downloaded
        to_download = list(map(lambda x: x, to_extract))
        for candidate in to_extract:
            zipfile = os.path.join(self.root, f"{candidate}.zip")
            if glob.glob(zipfile):
                print(f"Extracting: {candidate}")
                self._extract(candidate)
                to_download.remove(candidate)
        
        # Check if there are still files to download
        if not to_download: 
            return

        # Check if the user requested to download the dataset
        if not self.download:
            raise DatasetNotFoundError(self)

        print("Downloading: ", to_download)
        for candidate in to_download:
            self._download(candidate)
            self._extract(candidate)

    def _verify_toy(self) -> None:
        """Change urls/paths/content/configs to toy dataset."""
        
        print("-" * 80)
        print("WARNING: Using toy dataset.")
        print("This dataset should be used for testing purposes only.")
        print("Disabling use_toy-flag when initializing the dataset will initialize the full dataset.")
        print("-" * 80)
                
        if os.path.isdir(os.path.join(self.root, "flair_2_toy_dataset")):
            print("Toy dataset downloaded and extracted already...")
            self.root = os.path.join(self.root, "flair_2_toy_dataset")
            return

        if os.path.isfile(os.path.join(self.root, "flair_2_toy_dataset.zip")):
            print("Extracting toy dataset...")
            self._extract("flair_2_toy_dataset")
            self.root = os.path.join(self.root, "flair_2_toy_dataset")
            return
        
        if not self.download:
            raise DatasetNotFoundError(self)
        
        self._download("flair_2_toy_dataset")
        self._extract("flair_2_toy_dataset")
        self.root = os.path.join(self.root, "flair_2_toy_dataset")
        
    def _download(self, url: str, suffix: str = ".zip") -> None:
        """Download the dataset."""
        download_url(
            os.path.join(self.url_prefix, f"{url}{suffix}"), self.root
        )

    def _extract(self, file_path: str) -> None:
        """Extract the dataset."""
        assert isinstance(self.root, str | os.PathLike)
        zipfile = os.path.join(self.root, f"{file_path}.zip")
        extract_archive(zipfile)

    def plot(
        self,
        sample: dict[str, Tensor],
        show_titles: bool = True,
        suptitle: str | None = None,
    ) -> Figure:
        """Plot a sample from the dataset.

        Args:
            sample: a sample return by :meth:`__getitem__`
            show_titles: flag indicating whether to show titles above each panel
            suptitle: optional suptitle to use for figure

        Returns:
            a matplotlib Figure with the rendered sample
        """
        rgb_indices = [self.all_bands.index(band) for band in self.rgb_bands]
        # Check if RGB bands are present in self.bands
        if not all([band in self.bands for band in self.rgb_bands]):
            raise RGBBandsMissingError()
        
        def normalize_plot(tensor: Tensor) -> Tensor:
            """Normalize the plot."""
            return (tensor - tensor.min()) / (tensor.max() - tensor.min())
        
        # Define a colormap for the classes
        cmap = ListedColormap([
            'cyan',        # building
            'lightgray',   # pervious surface
            'darkgray',    # impervious surface
            'saddlebrown', # bare soil
            'blue',        # water
            'darkgreen',   # coniferous
            'forestgreen', # deciduous
            'olive',       # brushwood
            'purple',      # vineyard
            'lime',        # herbaceous vegetation
            'yellow',      # agricultural land
            'orange',      # plowed land
            'red'          # other
        ])
            
        # Stretch to the full range of the image
        image = normalize_plot(sample['image'][rgb_indices].permute(1, 2, 0))
        
        # Get elevation and NIR, R, G if available
        if "B05" in self.bands:
            elevation = sample['image'][self.bands.index("B05")]
        if "B04" in self.bands:
            nir_r_g_indices = [self.bands.index("B04"), rgb_indices[0], rgb_indices[1]]
            nir_r_g = normalize_plot(sample['image'][nir_r_g_indices].permute(1, 2, 0))
        
        # Sentinel is a time-series, i.e. use [0]->data(not snow_cloud_mask), [0]->T=0
        sentinel, cropping_indices = sample["sentinel"]
        sentinel = sentinel[0][0]
        sentinel = normalize_plot(sentinel[[0, 1, 2], :, :].permute(1, 2, 0))
        
        # Obtain mask and predictions if available
        mask = sample['mask'].numpy().astype('uint8').squeeze()
        
        showing_predictions = 'prediction' in sample
        predictions = None
        if showing_predictions:
            predictions = sample['prediction'].numpy().astype('uint8').squeeze()

        # Remove none available plots
        plots = zip(["image (R+G+B)", "NIR+R+G", "elevation", "sentinel", "predictions", "mask"], 
                    [image, nir_r_g, elevation, sentinel, predictions, mask])
        plots = [plot for plot in plots if plot[1] is not None]
        
        num_panels = len(plots)

        kwargs = {"cmap": cmap, 'vmin': 0, 'vmax': len(self.classes), 'interpolation': 'none'}
        fig, axs = plt.subplots(1, num_panels, figsize=(num_panels * 4, 5))
        
        for plot in plots:
            im_kwargs = kwargs.copy() if plot[0] == "mask" or plot[0] == "predictions" else {}
            if plot[0] == "sentinel":
                axs[0].add_patch(plt.Rectangle(
                    (cropping_indices[0].start, cropping_indices[1].start),
                    cropping_indices[0].stop - cropping_indices[0].start,
                    cropping_indices[1].stop - cropping_indices[1].start,
                    fill=False, edgecolor='red', lw=0.5))
                #axs[0].add_patch(plt.Rectangle(cropping_indices, self.super_patch_size, self.super_patch_size, fill=False, edgecolor='red', lw=0.2))
                #axs[0].add_patch(plt.Rectangle((x - eigth_size, y - eigth_size), quarter_size, quarter_size, fill=False, edgecolor='red', lw=0.2))

            axs[0].imshow(plot[1], **im_kwargs)
            axs[0].axis('off')
            if show_titles:
                axs[0].set_title(plot[0])
            
            axs = axs[1:]

        if suptitle is not None:
            plt.suptitle(suptitle)
        
        # Create a legend for the mask
        if "mask" in [plot[0] for plot in plots]:
            # Create a legend with class names
            legend_elements = [Patch(facecolor=cmap(i), edgecolor='k', label=cls) for i, cls in enumerate(self.classes)]
            fig.legend(handles=legend_elements, loc='upper left', bbox_to_anchor=(0.92, 0.85), fontsize='large')

        return fig
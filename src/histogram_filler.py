"""Module to handle the loading of histogram data from ROOT TTrees."""

import csv
from dataclasses import dataclass, field
import numpy as np
import ROOT
import utils
import histogram_manager as hm


@dataclass
class Config:
    """Configuration for histogram filling."""

    numch: int
    fp_length: float
    data_format: str


@dataclass
class HistogramDefinition:
    """Definition of the histograms to be created."""

    axis_labels: list[str]
    bins: list[dict]
    columns: list[str]
    gate: str
    name: str
    sum_det: bool


@dataclass
class HistogramFiller:
    """Fills histograms from a ROOT RDataFrame based on given configurations."""

    df: ROOT.RDataFrame
    config_path: str
    hist_def_path: str
    calib_path: str
    det_info_path: str
    instance_id: int = field(init=False)
    config: Config = field(init=False)
    hist_def: dict = field(init=False)
    calib_slope: np.ndarray = field(init=False)
    calib_offset: np.ndarray = field(init=False)
    active_ch_list: list = field(init=False)

    instance_num = 0

    def __post_init__(self):
        """Initializes the HistogramFiller instance."""
        self.instance_id = __class__.instance_num
        __class__.instance_num += 1

        try:
            self.config = self._load_config(self.config_path)
            self.hist_def = self._load_histogram_definitions(self.hist_def_path)
            self.det_map = utils.load_det_map(self.det_info_path)
            self.active_ch_list = self.det_map["active"]

            self.calib_slope, self.calib_offset = self._extract_calib_from_csv(
                self.calib_path, self.config.numch
            )

            self._validate_dataframe()

        except Exception as e:
            raise RuntimeError(f"Error initializing HistogramFiller: {e}")

        calib_slope_str = "{" + ",".join(map(str, self.calib_slope.tolist())) + "}"
        calib_offset_str = "{" + ",".join(map(str, self.calib_offset.tolist())) + "}"

        ROOT.gInterpreter.Declare(
            f"""
        std::vector<double> calib_slope_{self.instance_id} = {calib_slope_str};
        std::vector<double> calib_offset_{self.instance_id} = {calib_offset_str};
        """
        )

    def _validate_dataframe(self):
        """Validates that the DataFrame has the correct columns and types."""
        expected_columns = {
            "Coin": "UInt_t",
            "Flags": "UInt_t",
            "PulseHeight": "UShort_t",
            "Timestamp": "ULong64_t",
            "detector": "Int_t",
            "nTrigger": "ULong64_t",
            "tof": "ULong64_t",
        }

        df_columns = list(self.df.GetColumnNames())
        missing_columns = set(expected_columns.keys()) - set(df_columns)
        if missing_columns:
            raise ValueError(f"Missing expected columns: {missing_columns}")

        for col, expected_type in expected_columns.items():
            actual_type = self.df.GetColumnType(col)
            if actual_type != expected_type:
                raise TypeError(
                    f"Column '{col}' has type '{actual_type}', expected '{expected_type}'"
                )

    def _load_config(self, config_path):
        """Loads configuration from a TOML file."""
        config_dict = utils.load_toml_to_dict(config_path)
        return Config(
            numch=config_dict["general"]["numch"],
            fp_length=config_dict["general"]["fp_length"],
            data_format=config_dict["general"]["data_format"],
        )

    def _load_histogram_definitions(self, hist_def_path):
        """Loads histogram definitions from a TOML file, supporting multi-dimensional histograms."""
        try:
            hist_def_dict = utils.load_toml_to_dict(hist_def_path)

            if "histograms" not in hist_def_dict:
                raise KeyError(f"Key 'histograms' is missing from {hist_def_path}")

            histogram_definitions = {}

            for key, val in hist_def_dict["histograms"].items():
                required_keys = ["columns", "gate", "sum_det"]
                for req_key in required_keys:
                    if req_key not in val:
                        raise KeyError(
                            f"Key '{req_key}' is missing in histogram '{key}'"
                        )

                columns = val["columns"]
                if not isinstance(columns, list):
                    raise TypeError(
                        f"Expected 'columns' to be a list in histogram '{key}'"
                    )

                axis_labels = []
                bins = []
                for col in columns:
                    if col not in hist_def_dict["columns"]:
                        raise KeyError(
                            f"Column '{col}' not defined in 'columns' section."
                        )

                    col_data = hist_def_dict["columns"][col]
                    if "axis_label" not in col_data or "bins" not in col_data:
                        raise KeyError(
                            f"Column '{col}' must define both 'axis_label' and 'bins'."
                        )

                    axis_labels.append(col_data["axis_label"])
                    bins.append(col_data["bins"])

                histogram_definitions[key] = HistogramDefinition(
                    axis_labels=axis_labels,
                    bins=bins,
                    columns=columns,
                    sum_det=val["sum_det"],
                    name=key,
                    gate=val["gate"],
                )

            return histogram_definitions

        except KeyError as ke:
            raise RuntimeError(f"Missing key in histogram definitions: {ke}")
        except TypeError as te:
            raise RuntimeError(f"Type error in histogram definitions: {te}")
        except Exception as e:
            raise RuntimeError(f"Error loading histogram definitions: {e}")

    def _extract_calib_from_csv(self, filename, numch):
        """Extracts calibration data from a CSV file."""
        calib_slope = np.ones(numch)
        calib_offset = np.zeros(numch)

        with open(filename, "r", encoding="utf-8") as file:
            reader = csv.reader(file)
            next(reader)
            for row in reader:
                index = int(row[0])
                calib_slope[index] = float(row[1])
                calib_offset[index] = float(row[2])

        return calib_slope, calib_offset

    def define_columns(self):
        """Defines new columns in the DataFrame based on calibration data."""
        self.df = (
            self.df.Define("tof_ns", "tof*10.0")
            .Alias("tof_10ns", "tof")
            .Define("tof_mus", "tof_ns/1000.0+0.0000001")
            .Define("En", "pow((72.3*21.5/(tof_ns/1000.0)),2)+0.00001")
            .Define(
                "Egam",
                f"PulseHeight*calib_slope_{self.instance_id}[detector]+calib_offset_{self.instance_id}[detector]",
            )
            .Define("Egam_rounded", "std::round(Egam)*1.0")
        )

    def filter_active_channels(self):
        """Filters out inactive channels from the DataFrame."""
        for ch, active in enumerate(self.active_ch_list):
            if not active:
                self.df = self.df.Filter(f"detector != {ch}")

    def _create_df_dict(self):
        """Creates a dictionary of DataFrames filtered by histogram definitions."""
        df_dict = {}
        for key, hist_def in self.hist_def.items():
            gate = hist_def.gate
            name = hist_def.name
            df_gate = self.df if gate == "" else self.df.Filter(gate)

            if hist_def.sum_det:
                df_dict[key] = df_gate
            else:
                df_dict[key] = {}
                for ch in range(self.config.numch):
                    df_dict[key][f"{name}_d{ch}"] = df_gate.Filter(f"detector == {ch}")

        return df_dict

    def _create_hist_dict(self, df_dict, hist_model_dict):
        """Creates a dictionary of histograms from the DataFrame dictionary."""
        hist_dict = {}
        for key, hist_model in hist_model_dict.items():
            column = self.hist_def[key].column

            if self.hist_def[key].sum_det:
                hist_dict[key] = df_dict[key].Histo1D(hist_model, column)
            else:
                hist_dict[key] = {}
                for ch in range(self.config.numch):
                    hist_dict[key][f"{key}_d{ch}"] = df_dict[key][
                        f"{key}_d{ch}"
                    ].Histo1D(hist_model[f"{key}_d{ch}"], column)

        return hist_dict

    # def _create_hist_dict_from_df(self):
    #     """Creates a dictionary of histograms directly from the DataFrame."""
    #     df_ch = [self.df.Filter(f"detector == {ch}") for ch in range(self.config.numch)]
    #     hist_dict = {}

    #     for key, hist_def in self.hist_def.items():
    #         name = hist_def.name
    #         axis_labels = hist_def.xaxis
    #         columns = hist_def.column
    #         gate = hist_def.gate
    #         bins = hist_def.bins
    #         down, up, width, rebin, var = (
    #             bins["down"],
    #             bins["up"],
    #             bins["width"],
    #             bins["rebin"],
    #             bins["var"],
    #         )

    #         if hist_def.column == "En" and var:
    #             xbins = utils.get_xbins_En(down, up, self.config.fp_length, rebin)
    #         else:
    #             xbins = utils.get_xbins_constant(down, up, width, rebin)

    #         if gate != "":
    #             df_gate_all = self.df.Filter(gate)
    #             df_gate_ch = [df_ch[ch].Filter(gate) for ch in range(self.config.numch)]
    #         else:
    #             df_gate_all = self.df
    #             df_gate_ch = df_ch

    #         if hist_def.sum_det:
    #             title = f"{name};{xaxis};{yaxis}"
    #             hist_model_all = ROOT.RDF.TH1DModel(name, title, len(xbins) - 1, xbins)
    #             hist_all = df_gate_all.Histo1D(hist_model_all, column)
    #             hist_dict[key] = hist_all
    #         else:
    #             hist_dict[key] = {}
    #             for ch in range(self.config.numch):
    #                 title = f"{name}_d{ch};{xaxis};{yaxis}"
    #                 scale = self.calib_slope[ch] if "hEgam" in name else 1
    #                 hist_model_ch = ROOT.RDF.TH1DModel(
    #                     f"{name}_d{ch}", title, len(xbins) - 1, xbins * scale
    #                 )
    #                 hist_ch = df_gate_ch[ch].Histo1D(hist_model_ch, column)
    #                 hist_dict[key][f"{name}_d{ch}"] = hist_ch

    #     return hist_dict

    def _create_hist_dict_from_df(self):
        """Creates a dictionary of histograms directly from the DataFrame."""
        df_ch = [self.df.Filter(f"detector == {ch}") for ch in range(self.config.numch)]
        hist_dict = {}

        for key, hist_def in self.hist_def.items():
            name = hist_def.name
            columns = hist_def.columns  # List of columns
            gate = hist_def.gate
            bins = hist_def.bins

            # Handling multi-dimensional histograms
            xbins_list = [
                utils.get_xbins_constant(b["down"], b["up"], b["width"], b["rebin"])
                for b in bins
            ]

            # Apply gate if it exists
            if gate != "":
                df_gate_all = self.df.Filter(gate)
                df_gate_ch = [df_ch[ch].Filter(gate) for ch in range(self.config.numch)]
            else:
                df_gate_all = self.df
                df_gate_ch = df_ch

            # Check if we are summing over detectors
            if hist_def.sum_det:
                if len(columns) == 1:  # 1D histogram
                    title = f"{name};{hist_def.axis_labels[0]};Counts"
                    hist_model_all = ROOT.RDF.TH1DModel(
                        name, title, len(xbins_list[0]) - 1, xbins_list[0]
                    )
                    hist_all = df_gate_all.Histo1D(hist_model_all, columns[0])
                    hist_dict[key] = hist_all

                elif len(columns) == 2:  # 2D histogram
                    title = (
                        f"{name};{hist_def.axis_labels[0]};{hist_def.axis_labels[1]}"
                    )
                    hist_model_all = ROOT.RDF.TH2DModel(
                        name,
                        title,
                        len(xbins_list[0]) - 1,
                        xbins_list[0],
                        len(xbins_list[1]) - 1,
                        xbins_list[1],
                    )
                    hist_all = df_gate_all.Histo2D(
                        hist_model_all, columns[0], columns[1]
                    )
                    hist_dict[key] = hist_all

                # Additional handling for more dimensions can be added here

            else:  # If not summing detectors, create histograms for each detector
                hist_dict[key] = {}
                for ch in range(self.config.numch):
                    if len(columns) == 1:  # 1D histogram
                        title = f"{name}_d{ch};{hist_def.axis_labels[0]};Counts"
                        scale = self.calib_slope[ch] if "hEgam" in name else 1
                        hist_model_ch = ROOT.RDF.TH1DModel(
                            f"{name}_d{ch}",
                            title,
                            len(xbins_list[0]) - 1,
                            xbins_list[0] * scale,
                        )
                        hist_ch = df_gate_ch[ch].Histo1D(hist_model_ch, columns[0])
                        hist_dict[key][f"{name}_d{ch}"] = hist_ch

                    elif len(columns) == 2:  # 2D histogram
                        title = f"{name}_d{ch};{hist_def.axis_labels[0]};{hist_def.axis_labels[1]}"
                        hist_model_ch = ROOT.RDF.TH2DModel(
                            f"{name}_d{ch}",
                            title,
                            len(xbins_list[0]) - 1,
                            xbins_list[0],
                            len(xbins_list[1]) - 1,
                            xbins_list[1],
                        )
                        hist_ch = df_gate_ch[ch].Histo2D(
                            hist_model_ch, columns[0], columns[1]
                        )
                        hist_dict[key][f"{name}_d{ch}"] = hist_ch

                    # Additional handling for more dimensions can be added here

        return hist_dict

    def create_hm_from_df(self):
        """Creates a HistogramManager instance from the DataFrame."""
        return hm.HistogramManager(self._create_hist_dict_from_df())

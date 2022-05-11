{
  "format_version": 2,
  "executors": [
    {
      "tasks": [
        "train"
      ],
      "executor": {
        "path": "trainer.SimpleTrainer",
        "args": {
          "dataset_path_env_var": "environment-variable-for-parent-dir-of-dataset",
          "epochs_per_round": 2,
          "slideextension": "svs",
          "overlap": 0,
          "workers": 32,
          "output_folder": "name-of-subdir-within-dataset-parent-dir-to-store-generated-tiles",
          "quality": 90,
          "tile_size": 299,
          "background": 25,
          "magnification": 5,
          "labels_file": "name-of-labels-file-within-dataset-parent-dir",
          "validation_split": 0.2,
          "flipmode": "horizontal_and_vertical",
          "num_epoch_per_auc_calc": 0,
          "tensorboard": "string-of-comma-separated-tensorboard-kwargs",
          "baseimage": "environment-variable-with-path-to-baseimage"
        }
      }
    }
  ],
  "task_result_filters": [],
  "task_data_filters": []
}

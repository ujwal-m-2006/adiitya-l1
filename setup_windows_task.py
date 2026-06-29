
#!/usr/bin/env python3
"""
Windows Task Scheduler Setup Script
Installs a task to run the solar flare forecasting pipeline every 5 minutes
"""

import os
import sys
from pathlib import Path

# Try to import pywin32, install if missing
try:
    import win32com.client
except ImportError:
    print("pywin32 not found, installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pywin32"])
    import win32com.client


def main():
    script_dir = Path(__file__).parent
    python_exe = sys.executable
    pipeline_script = script_dir / "00_run_pipeline.py"

    task_name = "Aditya-L1 Solar Flare Forecasting"
    task_description = "Runs the Aditya-L1 solar flare forecasting pipeline every 5 minutes"

    print(f"Setting up Windows Task Scheduler task: {task_name}")

    try:
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()

        # Delete existing task if it exists
        root_folder = scheduler.GetFolder("\\")
        try:
            root_folder.DeleteTask(task_name, 0)
            print("Deleted existing task")
        except Exception:
            print("No existing task to delete")

        # Create new task
        task_def = scheduler.NewTask(0)

        # Set registration info
        task_def.RegistrationInfo.Description = task_description
        task_def.RegistrationInfo.Author = "Aditya-L1 Pipeline"

        # Set triggers - daily, every 5 minutes
        trigger = task_def.Triggers.Create(2)  # Daily trigger
        trigger.StartBoundary = "2024-01-01T00:00:00"
        trigger.Repetition.Interval = "PT5M"  # Every 5 minutes
        trigger.Repetition.Duration = "P1D"  # Repeat for 1 day
        trigger.Enabled = True

        # Set action
        action = task_def.Actions.Create(0)  # Executable action
        action.Path = python_exe
        action.Arguments = f'"{pipeline_script}"'
        action.WorkingDirectory = str(script_dir)

        # Set settings
        task_def.Settings.Enabled = True
        task_def.Settings.StartWhenAvailable = True
        task_def.Settings.RestartInterval = "PT5M"
        task_def.Settings.RestartCount = 3
        task_def.Settings.DisallowStartIfOnBatteries = False
        task_def.Settings.StopIfGoingOnBatteries = False

        # Register task
        root_folder.RegisterTaskDefinition(
            task_name,
            task_def,
            6,  # TASK_CREATE_OR_UPDATE
            "",  # No user
            "",  # No password
            0  # TASK_LOGON_NONE
        )

        print("Task successfully created!")
        print(f"Python exe: {python_exe}")
        print(f"Pipeline script: {pipeline_script}")

    except Exception as e:
        print(f"Error creating task: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

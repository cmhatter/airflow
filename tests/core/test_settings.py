#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock
from unittest.mock import MagicMock, call, patch

import pytest

from airflow.exceptions import AirflowClusterPolicyViolation, AirflowConfigException
from tests.test_utils.config import conf_vars

SETTINGS_FILE_POLICY = """
def test_policy(task_instance):
    task_instance.run_as_user = "myself"
"""

SETTINGS_FILE_POLICY_WITH_DUNDER_ALL = """
__all__ = ["test_policy"]

def test_policy(task_instance):
    task_instance.run_as_user = "myself"

def not_policy():
    print("This shouldn't be imported")
"""

SETTINGS_FILE_POD_MUTATION_HOOK = """
def pod_mutation_hook(pod):
    pod.namespace = 'airflow-tests'
"""

SETTINGS_FILE_CUSTOM_POLICY = """
from airflow.models.baseoperator import BaseOperator
from airflow.exceptions import AirflowClusterPolicyViolation

def task_must_have_owners(task: BaseOperator):
    if not task.owner or task.owner.lower() == "airflow":
        raise AirflowClusterPolicyViolation(
            f'''Task must have non-None non-'airflow' owner.
            Current value: {task.owner}'''
        )
"""


class SettingsContext:
    def __init__(self, content: str, module_name: str):
        self.content = content
        self.settings_root = tempfile.mkdtemp()
        filename = f"{module_name}.py"
        self.settings_file = os.path.join(self.settings_root, filename)

    def __enter__(self):
        with open(self.settings_file, "w") as handle:
            handle.writelines(self.content)
        sys.path.append(self.settings_root)
        return self.settings_file

    def __exit__(self, *exc_info):
        sys.path.remove(self.settings_root)


class TestLocalSettings:
    # Make sure that the configure_logging is not cached
    def setup_method(self):
        self.old_modules = dict(sys.modules)

    def teardown_method(self):
        # Remove any new modules imported during the test run. This lets us
        # import the same source files for more than one test.
        for mod in [m for m in sys.modules if m not in self.old_modules]:
            del sys.modules[mod]

    @mock.patch("airflow.settings.import_local_settings")
    @mock.patch("airflow.settings.prepare_syspath")
    def test_initialize_order(self, prepare_syspath, import_local_settings):
        """
        Tests that import_local_settings is called after prepare_classpath
        """
        mock_local_settings = mock.Mock()
        mock_local_settings.attach_mock(prepare_syspath, "prepare_syspath")
        mock_local_settings.attach_mock(import_local_settings, "import_local_settings")

        import airflow.settings

        airflow.settings.initialize()

        mock_local_settings.assert_has_calls([call.prepare_syspath(), call.import_local_settings()])

    def test_import_with_dunder_all_not_specified(self):
        """
        Tests that if __all__ is specified in airflow_local_settings,
        only module attributes specified within are imported.
        """
        with SettingsContext(SETTINGS_FILE_POLICY_WITH_DUNDER_ALL, "airflow_local_settings"):
            from airflow import settings

            settings.import_local_settings()

            with pytest.raises(AttributeError):
                settings.not_policy()

    def test_import_with_dunder_all(self):
        """
        Tests that if __all__ is specified in airflow_local_settings,
        only module attributes specified within are imported.
        """
        with SettingsContext(SETTINGS_FILE_POLICY_WITH_DUNDER_ALL, "airflow_local_settings"):
            from airflow import settings

            settings.import_local_settings()

            task_instance = MagicMock()
            settings.test_policy(task_instance)

            assert task_instance.run_as_user == "myself"

    @mock.patch("airflow.settings.log.debug")
    def test_import_local_settings_without_syspath(self, log_mock):
        """
        Tests that an ImportError is raised in import_local_settings
        if there is no airflow_local_settings module on the syspath.
        """
        from airflow import settings

        settings.import_local_settings()
        log_mock.assert_called_once_with("No airflow_local_settings to import.", exc_info=True)

    def test_policy_function(self):
        """
        Tests that task instances are mutated by the policy
        function in airflow_local_settings.
        """
        with SettingsContext(SETTINGS_FILE_POLICY, "airflow_local_settings"):
            from airflow import settings

            settings.import_local_settings()

            task_instance = MagicMock()
            settings.test_policy(task_instance)

            assert task_instance.run_as_user == "myself"

    def test_pod_mutation_hook(self):
        """
        Tests that pods are mutated by the pod_mutation_hook
        function in airflow_local_settings.
        """
        with SettingsContext(SETTINGS_FILE_POD_MUTATION_HOOK, "airflow_local_settings"):
            from airflow import settings

            settings.import_local_settings()

            pod = MagicMock()
            settings.pod_mutation_hook(pod)

            assert pod.namespace == "airflow-tests"

    def test_custom_policy(self):
        with SettingsContext(SETTINGS_FILE_CUSTOM_POLICY, "airflow_local_settings"):
            from airflow import settings

            settings.import_local_settings()

            task_instance = MagicMock()
            task_instance.owner = "airflow"
            with pytest.raises(AirflowClusterPolicyViolation):
                settings.task_must_have_owners(task_instance)


class TestUpdatedConfigNames:
    @conf_vars(
        {("webserver", "session_lifetime_days"): "5", ("webserver", "session_lifetime_minutes"): "43200"}
    )
    def test_updates_deprecated_session_timeout_config_val_when_new_config_val_is_default(self):
        from airflow import settings

        with pytest.warns(DeprecationWarning):
            session_lifetime_config = settings.get_session_lifetime_config()
            minutes_in_five_days = 5 * 24 * 60
            assert session_lifetime_config == minutes_in_five_days

    @conf_vars(
        {("webserver", "session_lifetime_days"): "5", ("webserver", "session_lifetime_minutes"): "43201"}
    )
    def test_uses_updated_session_timeout_config_when_val_is_not_default(self):
        from airflow import settings

        session_lifetime_config = settings.get_session_lifetime_config()
        assert session_lifetime_config == 43201

    @conf_vars({("webserver", "session_lifetime_days"): ""})
    def test_uses_updated_session_timeout_config_by_default(self):
        from airflow import settings

        session_lifetime_config = settings.get_session_lifetime_config()
        default_timeout_minutes = 30 * 24 * 60
        assert session_lifetime_config == default_timeout_minutes


def test_sqlite_relative_path():
    from airflow import settings

    with patch("airflow.settings.conf.get") as conf_get_mock:
        conf_get_mock.return_value = "sqlite:///./relative_path.db"
        with pytest.raises(AirflowConfigException) as exc:
            settings.configure_vars()
        assert "Cannot use relative path:" in str(exc.value)

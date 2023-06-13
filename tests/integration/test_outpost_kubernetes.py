"""outpost tests"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from kubernetes.client import AppsV1Api
from kubernetes.client.exceptions import OpenApiException

from authentik.core.tests.utils import create_test_flow
from authentik.lib.config import CONFIG
from authentik.outposts.controllers.k8s.deployment import DeploymentReconciler
from authentik.outposts.controllers.k8s.triggers import NeedsUpdate
from authentik.outposts.models import KubernetesServiceConnection, Outpost, OutpostType
from authentik.outposts.tasks import outpost_connection_discovery
from authentik.providers.proxy.controllers.kubernetes import ProxyKubernetesController
from authentik.providers.proxy.models import ProxyProvider


class OutpostKubernetesTests(TestCase):
    """Test Kubernetes Controllers"""

    def setUp(self):
        super().setUp()
        # Ensure that local connection have been created
        outpost_connection_discovery()  # pylint: disable=no-value-for-parameter
        self.provider: ProxyProvider = ProxyProvider.objects.create(
            name="test",
            internal_host="http://localhost",
            external_host="http://localhost",
            authorization_flow=create_test_flow(),
        )
        self.service_connection = KubernetesServiceConnection.objects.first()
        self.outpost: Outpost = Outpost.objects.create(
            name="test",
            type=OutpostType.PROXY,
            service_connection=self.service_connection,
        )
        self.outpost.providers.add(self.provider)
        self.outpost.save()

    def test_deployment_reconciler(self):
        """test that deployment requires update"""
        controller = ProxyKubernetesController(self.outpost, self.service_connection)
        deployment_reconciler = DeploymentReconciler(controller)

        self.assertIsNotNone(deployment_reconciler.retrieve())

        config = self.outpost.config
        config.kubernetes_replicas = 3
        # pylint: disable=line-too-long
        config.kubernetes_affinity = """{"nodeAffinity":{"preferredDuringSchedulingIgnoredDuringExecution":[{"preference":{"matchExpressions":[{"key":"node-label-key","operator":"In","values":["node-label-value"]}]},"weight":1}],"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"topology.kubernetes.io/region","operator":"In","values":["east1"]},{"key":"topology.kubernetes.io/zone","operator":"In","values":["us-east-1c"]}]}]}}}"""  # noqa: E501
        config.kubernetes_toleration = """{"tolerations":[{"key":"is_cpu_compute","operator":"Exists"},{"key":"is_gpu_compute","operator":"Exists"}]}"""  # noqa: E501
        config.kubernetes_resources = """{"resources":{"requests":{"cpu":"2","memory":"4Gi"},"limits":{"cpu":"4","memory":"8Gi"}}}"""  # noqa: E501
        self.outpost.config = config

        with self.assertRaises(NeedsUpdate):
            deployment_reconciler.reconcile(
                deployment_reconciler.retrieve(),
                deployment_reconciler.get_reference_object(),
            )

        with CONFIG.patch("outposts.container_image_base", "test"):
            with self.assertRaises(NeedsUpdate):
                deployment_reconciler.reconcile(
                    deployment_reconciler.retrieve(),
                    deployment_reconciler.get_reference_object(),
                )

        deployment_reconciler.delete(deployment_reconciler.get_reference_object())

    def test_controller_rename(self):
        """test that objects get deleted and re-created with new names"""
        controller = ProxyKubernetesController(self.outpost, self.service_connection)

        self.assertIsNone(controller.up())
        self.outpost.name = "foo"
        self.assertIsNone(controller.up())
        apps = AppsV1Api(controller.client)
        with self.assertRaises(OpenApiException):
            apps.read_namespaced_deployment("test", self.outpost.config.kubernetes_namespace)
        controller.down()

    def test_controller_full_update(self):
        """Test an update that triggers all objects"""
        controller = ProxyKubernetesController(self.outpost, self.service_connection)

        self.assertIsNone(controller.up())
        with patch(
            "authentik.outposts.controllers.k8s.base.get_version", MagicMock(return_value="1234")
        ):
            self.assertIsNone(controller.up())
        deployment_reconciler = DeploymentReconciler(controller)
        deployment = deployment_reconciler.retrieve()
        self.assertEqual(deployment.metadata.labels["app.kubernetes.io/version"], "1234")
        controller.down()

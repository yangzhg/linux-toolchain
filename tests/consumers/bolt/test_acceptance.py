# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import unittest

from consumers.bolt import acceptance


class BoltAcceptancePlanTest(unittest.TestCase):
    def parse(self, *arguments: str):
        return acceptance._parser().parse_args(arguments)

    def test_default_plan_builds_and_audits_bolt_as_an_external_consumer(self) -> None:
        args = self.parse(
            "--bolt-checkout",
            "/bolt",
            "--binding",
            "/binding",
            "--conan-home",
            "/conan",
            "--dry-run",
        )

        commands, audit_paths, conan_home = acceptance._commands(args)

        self.assertEqual(
            [step for step, _ in commands],
            ["doctor", "binding smoke", "Bolt build", "Bolt artifact audit"],
        )
        self.assertIn("PROFILE=/binding/conan/host.profile", commands[2][1])
        self.assertEqual([str(path) for path in audit_paths], ["/bolt/_build/Release"])
        self.assertEqual(str(conan_home), "/conan")

    def test_build_requires_an_explicit_prepared_conan_home(self) -> None:
        args = self.parse(
            "--bolt-checkout",
            "/bolt",
            "--binding",
            "/binding",
        )

        with self.assertRaisesRegex(acceptance.AcceptanceError, "--conan-home"):
            acceptance._commands(args)


if __name__ == "__main__":
    unittest.main()

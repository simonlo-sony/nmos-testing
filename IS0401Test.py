# Copyright (C) 2018 Riedel Communications GmbH & Co. KG
#
# Modifications Copyright 2018 British Broadcasting Corporation
#
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

import time
import socket
import json

from zeroconf_monkey import ServiceBrowser, ServiceInfo, Zeroconf
from MdnsListener import MdnsListener
from GenericTest import GenericTest, NMOSTestException, NMOS_WIKI_URL
from IS04Utils import IS04Utils
from Config import ENABLE_DNS_SD, QUERY_API_HOST, QUERY_API_PORT, DNS_SD_MODE, DNS_SD_ADVERT_TIMEOUT, HEARTBEAT_INTERVAL
from Config import ENABLE_HTTPS
from TestHelper import get_default_ip

NODE_API_KEY = "node"


class IS0401Test(GenericTest):
    """
    Runs IS-04-01-Test
    """
    def __init__(self, apis, registries, node, dns_server):
        GenericTest.__init__(self, apis)
        self.registries = registries
        self.node = node
        self.dns_server = dns_server
        self.node_url = self.apis[NODE_API_KEY]["url"]
        self.registry_basics_done = False
        self.registry_basics_data = []
        self.is04_utils = IS04Utils(self.node_url)
        self.zc = None
        self.zc_listener = None

    def set_up_tests(self):
        self.zc = Zeroconf()
        self.zc_listener = MdnsListener(self.zc)
        if self.dns_server:
            self.dns_server.load_zone(self.apis[NODE_API_KEY]["version"], self.protocol)

    def tear_down_tests(self):
        if self.zc:
            self.zc.close()
            self.zc = None
        if self.dns_server:
            self.dns_server.reset()

    def _registry_mdns_info(self, port, priority=0):
        """Get an mDNS ServiceInfo object in order to create an advertisement"""
        # TODO: Add another test which checks support for parsing CSV string in api_ver
        txt = {'api_ver': self.apis[NODE_API_KEY]["version"], 'api_proto': self.protocol, 'pri': str(priority)}

        service_type = "_nmos-registration._tcp.local."
        if self.is04_utils.compare_api_version(self.apis[NODE_API_KEY]["version"], "v1.3") >= 0:
            service_type = "_nmos-register._tcp.local."

        info = ServiceInfo(service_type,
                           "NMOSTestSuite{}.{}".format(port, service_type),
                           socket.inet_aton(get_default_ip()), port, 0, 0,
                           txt, "nmos-mocks.local.")
        return info

    def do_registry_basics_prereqs(self):
        """Advertise a registry and collect data from any Nodes which discover it"""

        if self.registry_basics_done or not ENABLE_DNS_SD:
            return

        if DNS_SD_MODE == "multicast":
            registry_mdns = []
            priority = 0
            for registry in self.registries:
                info = self._registry_mdns_info(registry.get_data().port, priority)
                registry_mdns.append(info)
                priority += 10

        # Reset all registries to clear previous heartbeats, etc.
        for registry in self.registries:
            registry.reset()

        registry = self.registries[0]
        self.registries[0].enable()

        if DNS_SD_MODE == "multicast":
            # Advertise a registry at pri 0 and allow the Node to do a basic registration
            self.zc.register_service(registry_mdns[0])

        # Wait for n seconds after advertising the service for the first POST from a Node
        time.sleep(DNS_SD_ADVERT_TIMEOUT)

        # Wait until we're sure the Node has registered everything it intends to, and we've had at least one heartbeat
        while (time.time() - self.registries[0].last_time) < HEARTBEAT_INTERVAL + 1:
            time.sleep(1)

        # Ensure we have two heartbeats from the Node, assuming any are arriving (for test_05)
        if len(self.registries[0].get_data().heartbeats) > 0:
            # It is heartbeating, but we don't have enough of them yet
            while len(self.registries[0].get_data().heartbeats) < 2:
                time.sleep(1)

            # Once registered, advertise all other registries at different (ascending) priorities
            for index, registry in enumerate(self.registries[1:]):
                registry.enable()
                if DNS_SD_MODE == "multicast":
                    self.zc.register_service(registry_mdns[index + 1])

            # Kill registries one by one to collect data around failover
            for index, registry in enumerate(self.registries):
                registry.disable()

                # Prevent access to an out of bounds index below
                if (index + 1) >= len(self.registries):
                    break

                # in event of testing HTTPS support, the TLS handshake seems to take nearly 2 seconds, so
                # when the first registry is disabled, an additional few seconds is needed to ensure the node
                # has a chance to make a connection to it, receive the 5xx error, and make a connection to
                # the next one
                if ENABLE_HTTPS:
                    heartbeat_countdown = HEARTBEAT_INTERVAL + 1 + 5
                else:
                    heartbeat_countdown = HEARTBEAT_INTERVAL + 1
                while len(self.registries[index + 1].get_data().heartbeats) < 1 and heartbeat_countdown > 0:
                    # Wait until the heartbeat interval has elapsed or a heartbeat has been received
                    time.sleep(1)
                    heartbeat_countdown -= 1

                if len(self.registries[index + 1].get_data().heartbeats) < 1:
                    # Testing has failed at this point, so we might as well abort
                    break

        # Clean up mDNS advertisements and disable registries
        for index, registry in enumerate(self.registries):
            if DNS_SD_MODE == "multicast":
                self.zc.unregister_service(registry_mdns[index])
            registry.disable()

        self.registry_basics_done = True
        for registry in self.registries:
            self.registry_basics_data.append(registry.get_data())

    def test_01(self, test):
        """Node can discover network registration service via multicast DNS"""

        if not ENABLE_DNS_SD or DNS_SD_MODE != "multicast":
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False or DNS_SD_MODE is not "
                                 "'multicast'")

        self.do_registry_basics_prereqs()

        registry_data = self.registry_basics_data[0]
        if len(registry_data.posts) > 0:
            return test.PASS()

        return test.FAIL("Node did not attempt to register with the advertised registry.")

    def test_02(self, test):
        """Node can discover network registration service via unicast DNS"""

        if not ENABLE_DNS_SD or DNS_SD_MODE != "unicast":
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False or DNS_SD_MODE is not "
                                 "'unicast'")

        self.do_registry_basics_prereqs()

        registry_data = self.registry_basics_data[0]
        if len(registry_data.posts) > 0:
            return test.PASS()

        return test.FAIL("Node did not attempt to register with the advertised registry.")

    def test_03(self, test):
        """Registration API interactions use the correct Content-Type"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        self.do_registry_basics_prereqs()

        registry_data = self.registry_basics_data[0]
        if len(registry_data.posts) == 0:
            return test.FAIL("No registrations found")

        for resource in registry_data.posts:
            if "Content-Type" not in resource[1]["headers"]:
                return test.FAIL("Node failed to signal its Content-Type correctly when registering.")
            elif resource[1]["headers"]["Content-Type"] != "application/json":
                return test.FAIL("Node signalled a Content-Type other than application/json.")

        return test.PASS()

    def get_registry_resource(self, res_type, res_id):
        found_resource = None
        if ENABLE_DNS_SD:
            # Look up data in local mock registry
            registry_data = self.registry_basics_data[0]
            for resource in registry_data.posts:
                if resource[1]["payload"]["type"] == res_type and resource[1]["payload"]["data"]["id"] == res_id:
                    found_resource = resource[1]["payload"]["data"]
        else:
            # Look up data from a configured Query API
            url = self.protocol + "://" + QUERY_API_HOST + ":" + str(QUERY_API_PORT) + "/x-nmos/query/" + \
                  self.apis[NODE_API_KEY]["version"] + "/" + res_type + "s/" + res_id
            try:
                valid, r = self.do_request("GET", url)
                if valid and r.status_code == 200:
                    found_resource = r.json()
                else:
                    raise Exception
            except Exception:
                print(" * ERROR: Unable to load resource from the configured Query API ({}:{})".format(QUERY_API_HOST,
                                                                                                       QUERY_API_PORT))
        return found_resource

    def get_node_resources(self, resp_json):
        resources = {}
        if isinstance(resp_json, dict):
            resources[resp_json["id"]] = resp_json
        else:
            for resource in resp_json:
                resources[resource["id"]] = resource
        return resources

    def check_matching_resource(self, test, res_type):
        if res_type == "node":
            url = "{}self".format(self.node_url)
        else:
            url = "{}{}s".format(self.node_url, res_type)
        # Get data from node itself
        valid, r = self.do_request("GET", url)
        if valid and r.status_code == 200:
            try:
                node_resources = self.get_node_resources(r.json())

                if len(node_resources) == 0:
                    return test.UNCLEAR("No {} resources were found on the Node.".format(res_type.title()))

                for res_id in node_resources:
                    reg_resource = self.get_registry_resource(res_type, res_id)
                    if not reg_resource:
                        return test.FAIL("{} {} was not found in the registry.".format(res_type.title(), res_id))
                    elif reg_resource != node_resources[res_id]:
                        return test.FAIL("Node API JSON does not match data in registry for "
                                         "{} {}.".format(res_type.title(), res_id))

                return test.PASS()
            except ValueError:
                return test.FAIL("Invalid JSON received!")
        else:
            return test.FAIL("Could not reach Node!")

    def test_04(self, test):
        """Node can register a valid Node resource with the network registration service,
        matching its Node API self resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "node")

    def test_05(self, test):
        """Node maintains itself in the registry via periodic calls to the health resource"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        api = self.apis[NODE_API_KEY]

        self.do_registry_basics_prereqs()

        registry_data = self.registry_basics_data[0]
        if len(registry_data.heartbeats) < 2:
            return test.FAIL("Not enough heartbeats were made in the time period.")

        initial_node = registry_data.posts[0]

        last_hb = None
        for heartbeat in registry_data.heartbeats:
            # Ensure the Node ID for heartbeats matches the registrations
            if heartbeat[1]["node_id"] != initial_node[1]["payload"]["data"]["id"]:
                return test.FAIL("Heartbeats matched a different Node ID to the initial registration.")

            if last_hb:
                # Check frequency of heartbeats matches the defaults
                time_diff = heartbeat[0] - last_hb[0]
                if time_diff > HEARTBEAT_INTERVAL + 0.5:
                    return test.FAIL("Heartbeats are not frequent enough.")
                elif time_diff < HEARTBEAT_INTERVAL - 0.5:
                    return test.FAIL("Heartbeats are too frequent.")
            else:
                # For first heartbeat, check against Node registration
                if (heartbeat[0] - initial_node[0]) > HEARTBEAT_INTERVAL + 0.5:
                    return test.FAIL("First heartbeat occurred too long after initial Node registration.")

            # Ensure the heartbeat request body is empty
            if heartbeat[1]["payload"] is not bytes():
                return test.WARNING("Heartbeat POST contained a payload body.",
                                    "https://amwa-tv.github.io/nmos-discovery-registration/branches/{}"
                                    "/docs/2.2._APIs_-_Client_Side_Implementation_Notes.html#empty-request-bodies"
                                    .format(api["spec_branch"]))
            if "Content-Type" in heartbeat[1]["headers"]:
                return test.WARNING("Heartbeat POST contained a Content-Type header.",
                                    "https://amwa-tv.github.io/nmos-discovery-registration/branches/{}"
                                    "/docs/2.2._APIs_-_Client_Side_Implementation_Notes.html#empty-request-bodies"
                                    .format(api["spec_branch"]))

            last_hb = heartbeat

        return test.PASS()

    def test_07(self, test):
        """Node can register a valid Device resource with the network registration service, matching its
        Node API Device resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "device")

    def test_08(self, test):
        """Node can register a valid Source resource with the network
        registration service, matching its Node API Source resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "source")

    def test_09(self, test):
        """Node can register a valid Flow resource with the network
        registration service, matching its Node API Flow resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "flow")

    def test_10(self, test):
        """Node can register a valid Sender resource with the network
        registration service, matching its Node API Sender resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "sender")

    def test_11(self, test):
        """Node can register a valid Receiver resource with the network
        registration service, matching its Node API Receiver resource"""

        self.do_registry_basics_prereqs()

        return self.check_matching_resource(test, "receiver")

    def test_12(self, test):
        """Node advertises a Node type mDNS announcement with no ver_* TXT records
        in the presence of a Registration API"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        registry = self.registries[0]
        registry_info = self._registry_mdns_info(registry.get_data().port, 0)

        # Reset the registry to clear previous data, although we won't be checking it
        registry.reset()
        registry.enable()

        if DNS_SD_MODE == "multicast":
            # Advertise a registry at pri 0 and allow the Node to do a basic registration
            self.zc.register_service(registry_info)

        # Wait for n seconds after advertising the service for the first POST from a Node
        time.sleep(DNS_SD_ADVERT_TIMEOUT)

        ServiceBrowser(self.zc, "_nmos-node._tcp.local.", self.zc_listener)
        time.sleep(1)
        node_list = self.zc_listener.get_service_list()

        # Withdraw the registry advertisement now we've performed a browse for Node advertisements
        if DNS_SD_MODE == "multicast":
            self.zc.unregister_service(registry_info)
        registry.disable()

        for node in node_list:
            address = socket.inet_ntoa(node.address)
            port = node.port
            api = self.apis[NODE_API_KEY]
            if address == api["ip"] and port == api["port"]:
                properties = self.convert_bytes(node.properties)
                for prop in properties:
                    if "ver_" in prop:
                        return test.FAIL("Found 'ver_' TXT record while Node is registered.")

                if self.is04_utils.compare_api_version(api["version"], "v1.1") >= 0:
                    if "api_ver" not in properties:
                        return test.FAIL("No 'api_ver' TXT record found in Node API advertisement.")
                    elif api["version"] not in properties["api_ver"].split(","):
                        return test.FAIL("Node does not claim to support version under test.")

                    if "api_proto" not in properties:
                        return test.FAIL("No 'api_proto' TXT record found in Node API advertisement.")
                    elif properties["api_proto"] != self.protocol:
                        return test.FAIL("API protocol ('api_proto') TXT record is not '{}'.".format(self.protocol))

                return test.PASS()

        return test.WARNING("No matching mDNS announcement found for Node with IP/Port {}:{}. This will not affect "
                            "operation in registered mode but may indicate a lack of support for peer to peer "
                            "operation.".format(self.apis[NODE_API_KEY]["ip"], self.apis[NODE_API_KEY]["port"]),
                            NMOS_WIKI_URL + "/IS-04#nodes-peer-to-peer-mode")

    def test_13(self, test):
        """PUTing to a Receiver target resource with a Sender resource payload is accepted
        and connects the Receiver to a stream"""

        valid, receivers = self.do_request("GET", self.node_url + "receivers")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(receivers))

        try:
            formats_tested = []
            for receiver in receivers.json():
                try:
                    stream_type = receiver["format"].split(":")[-1]
                except TypeError:
                    return test.FAIL("Unexpected Receiver format: {}".format(receiver))

                # Test each available receiver format once
                if stream_type in formats_tested:
                    continue

                if stream_type not in ["video", "audio", "data", "mux"]:
                    return test.FAIL("Unexpected Receiver format: {}".format(receiver["format"]))

                request_data = self.node.get_sender(stream_type)
                self.do_receiver_put(test, receiver["id"], request_data)

                # TODO: Define the sleep time globally for all connection tests
                time.sleep(1)

                valid, response = self.do_request("GET", self.node_url + "receivers/" + receiver["id"])
                if not valid:
                    return test.FAIL("Unexpected response from the Node API: {}".format(receiver))

                receiver = response.json()
                if receiver["subscription"]["sender_id"] != request_data["id"]:
                    return test.FAIL("Node API Receiver {} subscription does not reflect the subscribed "
                                     "Sender ID".format(receiver["id"]))

                api = self.apis[NODE_API_KEY]
                if self.is04_utils.compare_api_version(api["version"], "v1.2") >= 0:
                    if not receiver["subscription"]["active"]:
                        return test.FAIL("Node API Receiver {} subscription does not indicate an active "
                                         "subscription".format(receiver["id"]))

                formats_tested.append(stream_type)

            if len(formats_tested) > 0:
                return test.PASS()
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        return test.UNCLEAR("Node API does not expose any Receivers")

    def test_14(self, test):
        """PUTing to a Receiver target resource with an empty JSON object payload is accepted and
        disconnects the Receiver from a stream"""

        valid, receivers = self.do_request("GET", self.node_url + "receivers")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(receivers))

        try:
            if len(receivers.json()) > 0:
                receiver = receivers.json()[0]
                self.do_receiver_put(test, receiver["id"], {})

                # TODO: Define the sleep time globally for all connection tests
                time.sleep(1)

                valid, response = self.do_request("GET", self.node_url + "receivers/" + receiver["id"])
                if not valid:
                    return test.FAIL("Unexpected response from the Node API: {}".format(receiver))

                receiver = response.json()
                if receiver["subscription"]["sender_id"] is not None:
                    return test.FAIL("Node API Receiver {} subscription does not reflect the subscribed "
                                     "Sender ID".format(receiver["id"]))

                api = self.apis[NODE_API_KEY]
                if self.is04_utils.compare_api_version(api["version"], "v1.2") >= 0:
                    if receiver["subscription"]["active"]:
                        return test.FAIL("Node API Receiver {} subscription does not indicate an inactive "
                                         "subscription".format(receiver["id"]))

                return test.PASS()
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        return test.UNCLEAR("Node API does not expose any Receivers")

    def test_15(self, test):
        """Node correctly selects a Registration API based on advertised priorities"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        self.do_registry_basics_prereqs()

        last_hb = None
        last_registry = None
        for registry_data in self.registry_basics_data:
            if len(registry_data.heartbeats) < 1:
                return test.FAIL("Node never made contact with registry advertised on port {}"
                                 .format(registry_data.port))

            first_hb_to_registry = registry_data.heartbeats[0]
            if last_hb:
                if first_hb_to_registry < last_hb:
                    return test.FAIL("Node sent a heartbeat to the registry on port {} before the registry on port {}, "
                                     "despite their priorities requiring the opposite behaviour"
                                     .format(registry_data.port, last_registry.port))

            last_hb = first_hb_to_registry
            last_registry = registry_data

        return test.PASS()

    def test_16(self, test):
        """Node correctly fails over between advertised Registration APIs when one fails"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        self.do_registry_basics_prereqs()

        for index, registry_data in enumerate(self.registry_basics_data):
            if len(registry_data.heartbeats) < 1:
                return test.FAIL("Node never made contact with registry advertised on port {}"
                                 .format(registry_data.port))

            if index > 0 and len(registry_data.posts) > 0:
                return test.FAIL("Node re-registered its resources when it failed over to a new registry, when it "
                                 "should only have issued a heartbeat")

        return test.PASS()

    def test_17(self, test):
        """All Node resources use different UUIDs"""

        uuids = set()
        valid, response = self.do_request("GET", self.node_url + "self")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            uuids.add(response.json()["id"])
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        for resource_type in ["devices", "sources", "flows", "senders", "receivers"]:
            valid, response = self.do_request("GET", self.node_url + resource_type)
            if not valid:
                return test.FAIL("Unexpected response from the Node API: {}".format(response))
            try:
                for resource in response.json():
                    if resource["id"] in uuids:
                        return test.FAIL("Duplicate ID '{}' found in Node API '{}' resource".format(resource["id"],
                                                                                                    resource_type))
                    uuids.add(resource["id"])
            except json.decoder.JSONDecodeError:
                return test.FAIL("Non-JSON response returned from Node API")

        return test.PASS()

    def test_18(self, test):
        """All Node clocks are unique, and relate to any visible Sources' clocks"""

        api = self.apis[NODE_API_KEY]
        if self.is04_utils.compare_api_version(api["version"], "v1.1") < 0:
            return test.NA("Clocks are not available until IS-04 v1.1")

        clocks = set()
        valid, response = self.do_request("GET", self.node_url + "self")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            for clock in response.json()["clocks"]:
                clock_name = clock["name"]
                if clock_name in clocks:
                    return test.FAIL("Duplicate clock name '{}' found in Node API self resource".format(clock_name))
                clocks.add(clock_name)
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        valid, response = self.do_request("GET", self.node_url + "sources")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            for source in response.json():
                clock_name = source["clock_name"]
                if clock_name not in clocks and clock_name is not None:
                    return test.FAIL("Source '{}' uses a non-existent clock name '{}'".format(source["id"], clock_name))
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        return test.PASS()

    def test_19(self, test):
        """All Node interfaces are unique, and relate to any visible Senders and Receivers' interface_bindings"""

        api = self.apis[NODE_API_KEY]
        if self.is04_utils.compare_api_version(api["version"], "v1.2") < 0:
            return test.NA("Interfaces are not available until IS-04 v1.2")

        interfaces = set()
        valid, response = self.do_request("GET", self.node_url + "self")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            for interface in response.json()["interfaces"]:
                interface_name = interface["name"]
                if interface_name in interfaces:
                    return test.FAIL("Duplicate interface name '{}' found in Node API self resource"
                                     .format(interface_name))
                interfaces.add(interface_name)
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        valid, response = self.do_request("GET", self.node_url + "senders")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            for sender in response.json():
                interface_bindings = sender["interface_bindings"]
                for interface_name in interface_bindings:
                    if interface_name not in interfaces:
                        return test.FAIL("Sender '{}' uses a non-existent interface name '{}'"
                                         .format(sender["id"], interface_name))
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        valid, response = self.do_request("GET", self.node_url + "receivers")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            for receiver in response.json():
                interface_bindings = receiver["interface_bindings"]
                for interface_name in interface_bindings:
                    if interface_name not in interfaces:
                        return test.FAIL("Receiver '{}' uses a non-existent interface name '{}'"
                                         .format(receiver["id"], interface_name))
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        return test.PASS()

    def test_20(self, test):
        """Node's resources correctly signal the current protocol"""

        service_href_warn = False
        device_href_warn = False

        api = self.apis[NODE_API_KEY]
        valid, response = self.do_request("GET", self.node_url + "self")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            node_self = response.json()
            if not node_self["href"].startswith(self.protocol + "://"):
                return test.FAIL("Node 'href' does not match the current protocol")
            if self.is04_utils.compare_api_version(api["version"], "v1.1") >= 0:
                for endpoint in node_self["api"]["endpoints"]:
                    if endpoint["protocol"] != self.protocol:
                        return test.FAIL("One or more Node 'endpoints' do not match the current protocol")
            for service in node_self["services"]:
                if service["href"].startswith("http") and not service["href"].startswith(self.protocol):
                    # Only warn about these at the end so that more major failures are flagged first
                    # Protocols other than HTTP may be used, so don't incorrectly flag those too
                    service_href_warn = True
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        if self.is04_utils.compare_api_version(api["version"], "v1.1") >= 0:
            valid, response = self.do_request("GET", self.node_url + "devices")
            if not valid:
                return test.FAIL("Unexpected response from the Node API: {}".format(response))
            try:
                node_devices = response.json()
                for device in node_devices:
                    for control in device["controls"]:
                        if control["href"].startswith("http") and not control["href"].startswith(self.protocol):
                            # Only warn about these at the end so that more major failures are flagged first
                            # Protocols other than HTTP may be used, so don't incorrectly flag those too
                            device_href_warn = True
            except json.decoder.JSONDecodeError:
                return test.FAIL("Non-JSON response returned from Node API")

        valid, response = self.do_request("GET", self.node_url + "senders")
        if not valid:
            return test.FAIL("Unexpected response from the Node API: {}".format(response))
        try:
            node_senders = response.json()
            for sender in node_senders:
                if sender["manifest_href"] != "" and not sender["manifest_href"].startswith(self.protocol):
                    return test.WARNING("One or more Sender 'manifest_href' values do not match the current protocol")
        except json.decoder.JSONDecodeError:
            return test.FAIL("Non-JSON response returned from Node API")

        if service_href_warn:
            return test.WARNING("One or more Node service 'href' values does not match the current protocol.")
        elif device_href_warn:
            return test.WARNING("One or more Device control 'href' values does not match the current protocol.")

        return test.PASS()

    def test_21(self, test):
        """Node correctly interprets a 200 code from a registry upon initial registration"""

        if not ENABLE_DNS_SD:
            return test.DISABLED("This test cannot be performed when ENABLE_DNS_SD is False")

        registry = self.registries[0]
        registry_info = self._registry_mdns_info(registry.get_data().port, 0)

        # Reset the registry to clear previous heartbeats, and enable in 200 test mode
        registry.reset()
        registry.enable(first_reg=True)

        if DNS_SD_MODE == "multicast":
            # Advertise a registry at pri 0 and allow the Node to do a basic registration
            self.zc.register_service(registry_info)

        # Wait for n seconds after advertising the service for the first POST from a Node
        time.sleep(DNS_SD_ADVERT_TIMEOUT)

        # By this point we should have had at least one Node POST and a corresponding DELETE
        if DNS_SD_MODE == "multicast":
            self.zc.unregister_service(registry_info)
        registry.disable()

        # Get the relevant Node ID
        url = "{}self".format(self.node_url)
        valid, r = self.do_request("GET", url)
        if valid and r.status_code == 200:
            try:
                # Check that a POST and DELETE match the Node's ID
                node_id = r.json()["id"]
                found_post = False
                for resource in registry.get_data().posts:
                    if resource[1]["payload"]["type"] == "node" and resource[1]["payload"]["data"]["id"] == node_id:
                        found_post = True
                if not found_post:
                    return test.FAIL("Node did not attempt to make contact with the registry")
                found_delete = False
                for resource in registry.get_data().deletes:
                    if resource[1]["type"] == "node" and resource[1]["id"] == node_id:
                        found_delete = True
                if not found_delete:
                    return test.FAIL("Node did not attempt to DELETE itself having encountered a 200 code on initial "
                                     "registration")
            except json.decoder.JSONDecodeError:
                return test.FAIL("Non-JSON response returned from Node API")
        else:
            return test.FAIL("Unexpected responses from Node API self resource")

        return test.PASS()

    def do_receiver_put(self, test, receiver_id, data):
        """Perform a PUT to the Receiver 'target' resource with the specified data"""

        valid, put_response = self.do_request("PUT", self.node_url + "receivers/" + receiver_id + "/target", data)
        if not valid:
            raise NMOSTestException(test.FAIL("Unexpected response from the Node API: {}".format(put_response)))

        if put_response.status_code == 501:
            api = self.apis[NODE_API_KEY]
            if self.is04_utils.compare_api_version(api["version"], "v1.3") >= 0:
                raise NMOSTestException(test.OPTIONAL("Node indicated that basic connection management is not "
                                                      "supported",
                                                      NMOS_WIKI_URL + "/IS-04#nodes-basic-connection-management"))
            else:
                raise NMOSTestException(test.WARNING("501 'Not Implemented' status code is not supported below API "
                                                     "version v1.3",
                                                     NMOS_WIKI_URL + "/IS-04#nodes-basic-connection-management"))
        elif put_response.status_code != 202:
            raise NMOSTestException(test.FAIL("Receiver target PATCH did not produce a 202 response code: "
                                              "{}".format(put_response.status_code)))

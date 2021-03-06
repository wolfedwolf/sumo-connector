#!/usr/bin/env python3
import os
import sys
import queue
import datetime
import threading
import uuid
from argparse import ArgumentParser
from optparse import OptionParser
from collections import namedtuple
import json
import matplotlib.path
import logging
logging.basicConfig(level=logging.INFO)
sys.path += [os.path.join(os.path.dirname(__file__), "..", "python-test-bed-adapter")]
from test_bed_adapter.options.test_bed_options import TestBedOptions
from test_bed_adapter import TestBedAdapter
if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))
    import edgesInDistricts
    import sumolib
    import traci
    import traci.constants as tc
else:
    sys.exit("please declare environment variable 'SUMO_HOME'")

def get_options():
    argParser = ArgumentParser()
    argParser.add_argument("--aggregated-output", dest="aggregatedOutput",
                     default="edgesOutput.xml", help="the file name of the edge-based output generated by SUMO", metavar="FILE")
    argParser.add_argument("--fcd-output", dest="fcdOutput",
                     default="fcd.output", help=" the file name of the fcd output generated by SUMO", metavar="FILE")
    argParser.add_argument("--static-jsonOutput",  action="store_true", dest="staticJsonOutput",
                     default=False, help="write SUMO's static outputs in JSON format")
    argParser.add_argument("--nogui", action="store_true",
                         default=False, help="run the command-line version of sumo")
    argParser.add_argument("-s", "--server",
                    default="localhost", help="define the server; other possible values: 'tb6.driver-testbed.eu:3561', 'driver-testbed.eu', '129.247.218.121'")
    argParser.add_argument("-v", "--verbose", action="store_true", dest="verbose",
                     default=False, help="tell me what you are doing")
    options = argParser.parse_args()
    return options

def checkWithin(poly, x, y):
    return matplotlib.path.Path(poly.shape, closed=True).contains_point((x, y))

AffectedArea = namedtuple('AffectedArea', ['begin', 'end', 'polygons', 'edges', 'tls', 'restriction'])

class SumoConnector:
    def __init__(self):
        self._options = get_options()
        self._queue = queue.Queue()
        self._net = None
        self._simTime = None
        self._deltaT = None
        self._config = None
        self._affected = []
        self._runningVehicles = {}
        self._resetRestriction = {}
        self._inserted = []

    def addToQueue(self, message):
        self._queue.put(message['decoded_value'][0])

    def handleConfig(self, config):
        self._config = config
        self._simTime = config["begin"]
        if self._options.nogui:
            sumoBinary = sumolib.checkBinary('sumo')
        else:
            sumoBinary = sumolib.checkBinary('sumo-gui')
        try:
            traci.start([sumoBinary, "-S", "-Q",
                                     "-c", config["configFile"],
                                     "--fcd-output", self._options.fcdOutput, 
                                     "--device.fcd.period",str(config["singleVehicle"]),  # todo: add an option for the period
                                     ], numRetries=3)
            self._deltaT = traci.simulation.getDeltaT() * 1000
            for file in sumolib.xml.parse(config["configFile"], 'net-file'):
                netfile = os.path.join(os.path.dirname(config["configFile"]), file.value)
                print (netfile)
            self._net = sumolib.net.readNet(netfile)
        except traci.exceptions.FatalTraCIError as e:
            print(e)

    def handleTime(self, time):
        if self._net is None or time["state"] != "Started":
            return
        trialTime = time["trialTime"]
        print(datetime.datetime.fromtimestamp(trialTime / 1000.))
        if self._simTime < 0:
            self._simTime = trialTime
        while trialTime > self._simTime and (self._config["end"] < 0 or self._simTime < self._config["end"]):
            traci.simulationStep()
            self._simTime += self._deltaT
            self.checkAffected()
            self.writeSingleVehicleOutput(self._config["singleVehicle"])
            resultMap = traci.vehicle.getAllSubscriptionResults()
            for vid, valMap in resultMap.items():
                self.sendItemData(vid, vid, valMap)
            for vid, lon, lat in self._inserted:
                if vid not in resultMap:
                    data = {"guid" : vid,
                            "name" : "%s %s" % (vid, valMap[tc.VAR_TYPE]),
                            "owner": "sumo",
                            "visibleForParticipant": True,
                            "movable": True,
                            "location": { "latitude": lat, "longitude": lon, "altitude": 0 },
                            "orientation": { "yaw": 0, "pitch": 0, "roll": 0 },
                            "velocity": { "yaw": 0, "pitch": 0, "magnitude": 0 }
                    }
                    self._test_bed_adapter.producer_managers["simulation_entity_item"].send_messages([data])
                    self._inserted.remove((vid, lon, lat))
                    break

    def handleAffectedArea(self, area):
        affectedTLSList = []
        affectedEdgeList = []

        # currently only consider one polygon for each area
        shape = [self._net.convertLonLat2XY(*point) for point in area["area"]["coordinates"][0][0]]
        polygons = [sumolib.shapes.polygon.Polygon(area["id"], shape=shape)]

        reader = edgesInDistricts.DistrictEdgeComputer(self._net)
        optParser = OptionParser()
        edgesInDistricts.fillOptions(optParser)
        edgeOptions, _ = optParser.parse_args([])
        reader.computeWithin(polygons, edgeOptions)

        # get the affected edges
        result = list(reader._districtEdges.values())
        if result:
            affectedEdgeList = result[0]  # there is only one district

        if area["trafficLightsBroken"]:
            affectedIntersections = set()
            for n in self._net.getNodes():
                x, y = n.getCoord()
                for poly in polygons:
                    if checkWithin(poly, x, y) and n.getType() == "traffic_light" and n.getID() not in affectedIntersections:
                        affectedIntersections.add(n.getID())
            # get the affected TLS
            for tls in self._net.getTrafficLights():
                if tls.getID() not in affectedTLSList:
                    for c in tls.getConnections():
                        for n in (c[0].getEdge().getFromNode(), c[0].getEdge().getToNode(), c[1].getEdge().getToNode()):
                            if n.getID() in affectedIntersections:
                                affectedTLSList.append(tls.getID())
                                break
        self._affected.append(AffectedArea(area["begin"], area["end"], polygons, affectedEdgeList, affectedTLSList, area["restriction"].split()))

    def checkAffected(self):
        for affected in self._affected:
            if self._simTime == affected.begin:
                # switch off the affected traffic lights
                for tlsId in affected.tls:
                    traci.trafficlight.setProgram(tlsId, "off")
                # set the vehicle restriction on each edges
                for edge in affected.edges:
                    for lane in edge.getLanes():
                        # save the original restrictions
                        self._resetRestriction[lane.getID()] = traci.lane.getDisallowed(lane.getID())
                        if 'all' in affected.restriction:
                            traci.lane.setAllowed(lane.getID(), ["authority"])
                        else:
                            traci.lane.setDisallowed(lane.getID(), affected.restriction)
                # subscribe variables
                # todo: wait for the new traci-function to get num_reroute, num _canNotReach and num_avgContained
                for pObj in affected.polygons: # currently only consider one polygon
                    traci.polygon.add(pObj.id, pObj.shape, (255, 0, 0), layer=100)
                    traci.polygon.subscribeContext(pObj.id, tc.CMD_GET_VEHICLE_VARIABLE, 10.,
                                                   [tc.VAR_VEHICLECLASS,
                                                    tc.VAR_POSITION,            # return sumo internal positions
                                                    tc.VAR_ROUTE_VALID]) 
#                                                    tc.VAR_REROUTE,             # to be built
#                                                    tc.VAR_CAN_NOT_REACH,       # to be built
#                                                    tc.VAR_AVERAGE_CONTAINED])  # to be built   ? check hoe to compute
                    # need to recheck whether all retrieved vehicles are really in the polygon (or only in the defined bounding box)

            # reset the TLS programs
            if self._simTime == affected.end:
                for tlsId in affected.tls:
                    tlsObj = self._net.getTLSSecure(tlsId)
                    for p in tlsObj.getPrograms().keys():  # only consider the first program
                        traci.trafficlight.setProgram(tlsId, p)
                        break
                for edge in affected.edges:
                    for lane in edge.getLanes():
                        traci.lane.setDisallowed(lane.getID(), self._resetRestriction[lane.getID()])

    def sendItemData(self, guid, vid, valMap):
        data = {"guid" : guid,
                "name" : "%s %s" % (vid, valMap[tc.VAR_TYPE]),
                "owner": "sumo",
                "visibleForParticipant": True,
                "movable": True}
        x, y, alt = valMap[tc.VAR_POSITION3D]
        lon, lat = self._net.convertXY2LonLat(x, y)
        data["location"] = { "latitude": lat, "longitude": lon, "altitude": alt }
        angle = valMap[tc.VAR_ANGLE]
        slope = valMap[tc.VAR_SLOPE]
        data["orientation"] = { "yaw": angle, "pitch": slope, "roll": 0 }
        data["velocity"] = { "yaw": angle, "pitch": slope, "magnitude": valMap[tc.VAR_SPEED] }
        self._test_bed_adapter.producer_managers["simulation_entity_item"].send_messages([data])

    def writeSingleVehicleOutput(self, samplePeriod):
        if samplePeriod <= 0:
            return
        for vid in traci.simulation.getDepartedIDList():
            traci.vehicle.subscribe(vid, [tc.VAR_TYPE, tc.VAR_POSITION3D, tc.VAR_ANGLE, tc.VAR_SLOPE, tc.VAR_SPEED])
            self._runningVehicles[vid] = str(uuid.uuid1())
        if self._simTime % samplePeriod == 0.:
            resultMap = traci.vehicle.getAllSubscriptionResults()
            for vid, valMap in resultMap.items():
                self.sendItemData(self._runningVehicles[vid], vid, valMap)

    def handleRoutingRequest(self, routing):
        guid = routing["unit"]
        startEdge, startPos, startLane = s = traci.simulation.convertRoad(routing["route"][0]["longitude"], routing["route"][0]["latitude"], True)
        endEdge, endPos, endLane = e = traci.simulation.convertRoad(routing["route"][1]["longitude"], routing["route"][1]["latitude"], True)
        print("routing from", s, "to",  e)
        traci.route.add(guid, (startEdge, endEdge))
        traci.vehicle.add(guid, guid, "ignoring", departPos=str(startPos), arrivalPos=str(endPos))#, departLane=str(startLane), arrivalLane=str(endLane))
        traci.vehicle.subscribe(guid, [tc.VAR_TYPE, tc.VAR_POSITION3D, tc.VAR_ANGLE, tc.VAR_SLOPE, tc.VAR_SPEED])
        self._inserted.append((guid, routing["route"][1]["longitude"], routing["route"][1]["latitude"]))

    def main(self):
        if ":" not in self._options.server:
            server = self._options.server
            port = 3501
        else:
            server, port = self._options.server.split(":")
        testbed_options = {
            "auto_register_schemas": True,
            "schema_folder": 'data/schemas',
            "kafka_host": "%s:%s" % (server, port),
            "schema_registry": 'http://%s:%s' % (server, int(port) + 1),
            "reset_offset_on_start": True,
            "offset_type": "LATEST",
            "client_id": 'SUMO Connector',
            "consume": ["sumo_SumoConfiguration", "sumo_AffectedArea", "system_timing", "simulation_request_unittransport"],
            "produce": ["simulation_entity_item"]}

        self._test_bed_adapter = TestBedAdapter(TestBedOptions(testbed_options))
        self._test_bed_adapter.on_message += self.addToQueue

        self._test_bed_adapter.initialize()
        threads = []
        for topic in testbed_options["consume"]:
            threads.append(threading.Thread(target=self._test_bed_adapter.consumer_managers[topic].listen_messages))
            threads[-1].start()
        while True:
            message = self._queue.get()
            logging.info("\n\n-----\nHandling message\n-----\n\n" + str(message))
            if "configFile" in message:
                self.handleConfig(message)
            elif "trialTime" in message:
                self.handleTime(message)
            elif "restriction" in message:
                self.handleAffectedArea(message)
            elif "route" in message:
                self.handleRoutingRequest(message)




if __name__ == '__main__':
    SumoConnector().main()

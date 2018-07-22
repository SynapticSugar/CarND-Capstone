#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
from scipy.spatial import KDTree
import tf
import cv2
import yaml


#Detector Stuff
import os
from cfg import *
from mobiledet.utils import utils
from mobiledet.models.keras_yolo import yolo_eval, decode_yolo_output, create_model
from keras import backend as K
import time

STATE_COUNT_THRESHOLD = 3

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        self.camera_image = None
        self.lights = []

        sub1 = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        sub2 = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        sub3 = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        sub6 = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)
        
        self.is_site = self.config['is_site']

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0
        self.waypoints_2d = None
        self.waypoint_tree = None
        self.waypoints = None
        
        self.ground_truth = True


        #Detector Stuff
        self.model_image_size = None
        self.sess = None
        model_path = os.path.expanduser('./weights/mobilenet_s2_best.FalseFalse.h5')
        anchors_path = os.path.expanduser('./model_data/lisa_anchors.txt')
        classes_path = os.path.expanduser('./model_data/lisa_classes.txt')
        
        class_names  = utils.get_classes(classes_path)
        anchors = utils.get_anchors(anchors_path)
        if SHALLOW_DETECTOR:
            anchors = anchors * 2
        
        print(class_names)
        print(anchors)
        
        self.yolo_model, yolo_model_for_training = create_model(anchors, class_names, load_pretrained=True, 
        feature_extractor=FEATURE_EXTRACTOR, pretrained_path=model_path, freeze_body=True)

        model_file_basename, file_extension = os.path.splitext(os.path.basename(model_path))

        model_input = self.yolo_model.input.name.replace(':0', '') # input_1
        model_output = self.yolo_model.output.name.replace(':0', '') # conv2d_3/BiasAdd

        sess = K.get_session()
        width, height, channels = int(self.yolo_model.input.shape[2]), int(self.yolo_model.input.shape[1]), int(self.yolo_model.input.shape[3])

        # END OF keras specific code

        # Check if model is fully convolutional, assuming channel last order.
        self.model_image_size = self.yolo_model.layers[0].input_shape[1:3]

        self.sess = K.get_session()  # TODO: Remove dependence on Tensorflow session.

        # Generate output tensor targets for filtered bounding boxes.
        yolo_outputs = decode_yolo_output(self.yolo_model.output, anchors, len(class_names))

        self.input_image_shape = K.placeholder(shape=(2, ))
        self.boxes, self.scores, self.classes = yolo_eval(
            yolo_outputs,
            self.input_image_shape,
            score_threshold=.6,
            iou_threshold=.6)


        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg

    def waypoints_cb(self, waypoints):
        self.waypoints = waypoints
        if not self.waypoints_2d:
            self.waypoints_2d = [[wp.pose.pose.position.x, wp.pose.pose.position.y] for wp in waypoints.waypoints]
            self.waypoint_tree = KDTree(self.waypoints_2d)


    def traffic_cb(self, msg):
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_waypoint(self, pose):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        #TODO implement
        x = pose.position.x
        y = pose.position.y
        closest_idx = self.waypoint_tree.query([x, y], 1)[1]
        return closest_idx

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        return light.state

        # if(not self.has_image):
        #     self.prev_light_loc = None
        #     return False

        # cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        # #Get classification
        # return self.light_classifier.get_classification(cv_image)

    def detect_traffic_light(self):
        """Determine the state of the traffic light in the scene (if any)
           Using a Yolo_V2 network to detect and classify in a single step.

        Args:
            None

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)
                 UNKNOWN if not found

        """
        # Lisa        styx_msgs/TrafficLight[] (uint8)
        # stop=0      RED=0
        # go=1        GREEN=2
        # warning=2   YELLOW=1
        # dontcare=3  UNKNOWN=4

        # if self.sess:
        #     cv_image = self.bridge.imgmsg_to_cv2(self.camera_image)
        #     # resized_image = cv_image.resize(
        #     #     tuple(reversed(self.model_image_size)))
        #     height, width, channels = cv_image.shape
        #     resized_image = cv2.resize(cv_image, tuple(reversed(self.model_image_size)))
        #     image_data = np.array(resized_image, dtype='float32')
        #     image_data /= 255.
        #     image_data = np.expand_dims(image_data, 0)  # Add batch dimension.
        #     start = time.time()
        #     out_boxes, out_scores, out_classes = self.sess.run(
        #         [self.boxes, self.scores, self.classes],
        #         feed_dict={
        #             self.yolo_model.input: image_data,
        #             self.input_image_shape: [width, height],
        #             K.learning_phase(): 0
        #         })
        #     last = (time.time() - start)
        #     print('{}: Found {} boxes for {}'.format(last, len(out_boxes), idx))

        # TODO return the actual detected state
        return TrafficLight.UNKNOWN

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming stop line for a traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light = None
        line_wp_idx = None

        # List of positions that correspond to the line to stop in front of for a given intersection
        stop_line_positions = self.config['stop_line_positions']
        if self.pose and self.waypoints and self.waypoint_tree:
            car_wp_idx = self.get_closest_waypoint(self.pose.pose)

            #TODO find the closest visible traffic light (if one exists)
            diff = len(self.waypoints.waypoints)
            for i, light in enumerate(self.lights):
                #Get stop line waypoint index
                line = stop_line_positions[i]
                p = Pose()
                p.position.x = line[0]
                p.position.y = line[1]
                temp_wp_idx = self.get_closest_waypoint(p)
                #Find closest stop line waypoint index
                d = temp_wp_idx - car_wp_idx
                if d>= 0 and d < diff:
                    diff = d;
                    closest_light = light
                    line_wp_idx = temp_wp_idx

        if self.ground_truth:
            if closest_light:
                return line_wp_idx, closest_light.state
        else:
            state = self.detect_traffic_light()
            if state != TrafficLight.UNKNOWN:
                return line_wp_idx, state

        return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')

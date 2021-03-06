# -*- coding: utf-8 -*-

import os
import sys
import time
import traceback
import numpy as np
import caffe
import cv2
import hashlib
from evals.utils import create_net_handler, net_preprocess_handler, net_inference_handler, CTX, \
    monitor_rt_load, monitor_rt_forward, monitor_rt_post
from evals.utils.error import *
from evals.utils.image import load_image


@create_net_handler
def create_net(configs):

    CTX.logger.info("load configs: %s", configs)
    caffe.set_mode_gpu()
    deploy = str(configs['model_files']["deploy.prototxt"])
    weight = str(configs['model_files']["weight.caffemodel"])

    net = caffe.Net(deploy, weight, caffe.TEST)
    if 'custom_params' in configs:
        custom_params = configs['custom_params']
        if 'threshold' in custom_params:
            thresholds = custom_params['thresholds']
        else:
            thresholds = [0, 0.1, 0.1, 0.1, 0.1, 0.1, 1.0]
    return {"net": net, "thresholds": thresholds, "batch_size": configs['batch_size']}, 0, ''


@net_preprocess_handler
def net_preprocess(model, req):
    CTX.logger.info("PreProcess...")
    return req, 0, ''


@net_inference_handler
def net_inference(model, reqs):

    net = model["net"]
    thresholds = model["thresholds"]
    batch_size = model['batch_size']
    CTX.logger.info("inference begin ...")
    try:
        image_shape_list_h_w, images = pre_eval(net, batch_size, reqs)
        output = eval(net, images)
        ret = post_eval(output, thresholds, image_shape_list_h_w)
    except ErrorBase as e:
        return [], e.code, str(e)
    except Exception as e:
        CTX.logger.error("inference error: %s", traceback.format_exc())
        return [], 599, str(e)
    return ret, 0, ''


def preProcessImage(oriImage=None):
    img = cv2.resize(oriImage, (320, 320))
    img = img.astype(np.float32, copy=False)
    img = img - np.array([[[103.52, 116.28, 123.675]]])
    img = img * 0.017
    img = img.astype(np.float32)
    img = img.transpose((2, 0, 1))
    return img


def pre_eval(net, batch_size, reqs):
    '''
        prepare net forward data
        Parameters
        ----------
        net: net created by net_init
        reqs: parsed reqs from net_inference
        reqid: reqid from net_inference
        Return
        ----------
        code: error code, int
        message: error message, string
    '''
    cur_batchsize = len(reqs)
    CTX.logger.info("cur_batchsize: %d\n", cur_batchsize)
    if cur_batchsize > batch_size:
        for i in range(cur_batchsize):
            raise ErrorOutOfBatchSize(batch_size)
    image_shape_list_h_w = []
    images = []
    _t1 = time.time()
    for i in range(cur_batchsize):
        data = reqs[i]
        img = load_image(data["data"]["uri"], body=data['data']['body'])
        if img is None:
            CTX.logger.info("input data is none : %s\n", data)
            raise ErrorBase(400, "image data is None ")
        height, width, _ = img.shape
        if img.ndim != 3:
            raise ErrorBase(400, "image ndim is " +
                            str(img.ndim) + ", should be 3")
        image_shape_list_h_w.append([height, width])
        images.append(preProcessImage(oriImage=img))
    _t2 = time.time()
    CTX.logger.info("read image and transform: %f\n", _t2 - _t1)
    monitor_rt_load().observe(_t2 - _t1)
    return image_shape_list_h_w, images


"""
item {
    name: "none_of_the_above"
    label: 0
    display_name: "background"
}
item {
    name: "guns"
    label: 1
    display_name: "guns"
}
item {
    name: "knives"
    label: 2
    display_name: "knives"
}
item {
    name: "tibetan flag"
    label: 3
    display_name: "tibetan flag"
}
item {
    name: "islamic flag"
    label: 4
    display_name: "islamic flag"
}
item {
    name: "isis flag"
    label: 5
    display_name: "isis flag"
}
item {
    name: "not terror"
    label: 6
    display_name: "not terror"
}
"""


def post_eval(output, thresholds, image_shape_list_h_w):
    '''
        parse net output, as numpy.mdarray, to EvalResponse
        Parameters
        ----------
        net: net created by net_init
        output: list of tuple(score, boxes)
        reqs: parsed reqs from net_inference
        reqid: reqid from net_inference
        Return
        ----------
        resps: list of EvalResponse{
            "code": <code|int>,
            "message": <error message|str>,
            "result": <eval result|object>,
            "result_file": <eval result file path|string>
        }
    '''
    # thresholds = [0,0.1,0.1,0.1,0.1,0.1,1.0]
    resps = []
    cur_batchsize = len(image_shape_list_h_w)
    _t1 = time.time()
    # output_bbox_list : bbox_count * 7
    output_bbox_list = output['detection_out'][0][0]
    image_result_dict = dict()  # image_id : bbox_list
    for i_bbox in output_bbox_list:
        image_id = int(i_bbox[0])
        if image_id >= cur_batchsize:
            break
        h = image_shape_list_h_w[image_id][0]
        w = image_shape_list_h_w[image_id][1]
        class_index = int(i_bbox[1])
        # [background,guns,knives,tibetan flag,islamic flag,isis flag,not terror]
        if class_index < 1 or class_index >= 6:
            continue
        score = float(i_bbox[2])
        if score < thresholds[class_index]:
            continue
        bbox_dict = dict()
        bbox_dict['index'] = class_index
        bbox_dict['score'] = score
        bbox = i_bbox[3:7] * np.array([w, h, w, h])
        bbox_dict['pts'] = []
        xmin = int(bbox[0]) if int(bbox[0]) > 0 else 0
        ymin = int(bbox[1]) if int(bbox[1]) > 0 else 0
        xmax = int(bbox[2]) if int(bbox[2]) < w else w
        ymax = int(bbox[3]) if int(bbox[3]) < h else h
        bbox_dict['pts'].append([xmin, ymin])
        bbox_dict['pts'].append([xmax, ymin])
        bbox_dict['pts'].append([xmax, ymax])
        bbox_dict['pts'].append([xmin, ymax])
        if image_id in image_result_dict.keys():
            the_image_bbox_list = image_result_dict.get(image_id)
            the_image_bbox_list.append(bbox_dict)
            pass
        else:
            the_image_bbox_list = []
            the_image_bbox_list.append(bbox_dict)
            image_result_dict[image_id] = the_image_bbox_list
    resps = []
    for image_id in range(cur_batchsize):
        if image_id in image_result_dict.keys():
            res_list = image_result_dict.get(image_id)
        else:
            res_list = []
        result = {"detections": res_list}
        resps.append({"code": 0, "message": "", "result": result})
    _t2 = time.time()
    CTX.logger.info("post: %f\n", _t2 - _t1)
    monitor_rt_post().observe(_t2 - _t1)
    return resps


def eval(net, images):
    '''
        eval forward inference
        Return
        ---------
        output: network numpy.mdarray
    '''
    _t1 = time.time()
    for index, i_data in enumerate(images):
            net.blobs['data'].data[index] = i_data
    _t2 = time.time()
    CTX.logger.info("load image to net: %f\n", _t2 - _t1)
    monitor_rt_forward().observe(_t2 - _t1)

    _t1 = time.time()
    output = net.forward()
    _t2 = time.time()
    CTX.logger.info("forward: %f\n", _t2 - _t1)
    monitor_rt_forward().observe(_t2 - _t1)

    CTX.logger.info('detection_out: {}'.format(output['detection_out']))

    if 'detection_out' not in output or len(output['detection_out']) < 1:
        raise ErrorForwardInference()
    return output

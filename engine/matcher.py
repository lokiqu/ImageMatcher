import cv2
import numpy as np
import math
import StringIO
from operator import itemgetter

from models.referenceimage import ReferenceImage
from common.metadata_extraction import extract_metadata
from common.image_helpers import generate_thumbnail
from common import dbcache
from profiling import timeit


# Constants
MIN_NUMBER_OF_FEATURES = 100
MIN_MATCH_COUNT = 5
MAX_REF_IMAGE_SIZE = 512
MAX_MATCH_IMAGE_SIZE = 1024
MAX_IMAGES_FOUND = 5

THUMBNAIL_SIZE = 800, 600

# ORB maximum number of features returned
ORB_MAX_FEATURES = 250


# Detectors and matchers
__detectors__ = None
__extractor__ = None
__matcher__ = None


def init_opencv():
    global __detectors__, __extractor__, __matcher__

    # Initiate SURF detector
    # min_hessian_import = 400
    # min_hessian_match = 400
    # surf_import = cv2.SURF(min_hessian_import)
    # surf_match = cv2.SURF(min_hessian_match)

    # BRISK detector
    # extractor = cv2.DescriptorExtractor_create('BRISK')
    # detector = cv2.BRISK(thresh=10, octaves=0)

    # extractor = cv2.SURF(1500, 4, 2, False)
    # detectors = [cv2.SURF(5000, 4, 2, False), extractor, cv2.SURF(400, 4, 2, False)]

    # ORB detector
    extractor = cv2.ORB(ORB_MAX_FEATURES)
    detectors = [extractor, extractor]

    # FLANN parameters
    # FLANN_INDEX_KDTREE = 1
    FLANN_INDEX_LSH = 6

    # For SURF
    # index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=4)

    # For ORB
    index_params = dict(algorithm=FLANN_INDEX_LSH,
                        table_number=6,  # 12
                        key_size=12,     # 20
                        multi_probe_level=1)  # 2

    search_params = dict(checks=50)   # or pass empty dictionary

    flann = cv2.FlannBasedMatcher(index_params, search_params)
    # flann = cv2.BFMatcher()
    # flann = indexed_descriptors.Matcher()

    __detectors__ = detectors
    __extractor__ = extractor
    __matcher__ = flann

    return detectors, extractor, flann


def train_matcher(ref_image, descriptors):
    dbcache.add(ref_image)
    __matcher__.add([descriptors])


def load_db_in_memory():
    dbcache.clear()
    for o in ReferenceImage.objects:
        ref_image = o.to_opencv_description()
        train_matcher(ref_image, ref_image[1])


def fit_image(img, max_border_size):
    # rescale to have the largest side at max_border_size
    y, x = img.shape

    if y > max_border_size or x > max_border_size:
        new_x, new_y = 0, 0

        if x > y:
            new_x = max_border_size
            new_y = y * new_x / x
        else:
            new_y = max_border_size
            new_x = x * new_y / y

        return cv2.resize(img, (new_x, new_y), interpolation=cv2.INTER_AREA)
    else:
        return img


@timeit
def open_image(file, max_border_size):
    # convert the data to an array for decoding
    # Go back to the begining of the stream, if needed
    file.seek(0)
    img_array = np.asarray(bytearray(file.read()), dtype=np.uint8)
    img = cv2.imdecode(img_array, 0)
    if img is None:
        return None

    img_resized = fit_image(img, max_border_size)
    return img_resized


@timeit
def detectAndComputeDescriptors(img):
    # Optimization: choose the detector according to the number of pixels
    height, width = img.shape
    surface = height * width
    sensitive = False
    if surface < 1024 * 512:
        sensitive = True

    # find the keypoints and descriptors with SURF
    kp = None
    i = 0
    list_detectors = __detectors__
    if sensitive:
        list_detectors = list_detectors[1:]

    for detector in list_detectors:
        i = i + 1
        kp = detector.detect(img, None)
        if len(kp) >= MIN_NUMBER_OF_FEATURES:
            print "Exit at " + str(i)
            break

    kp, des = __extractor__.compute(img, kp)

    print "Number of descriptors: " + str(len(kp))
    return kp, des


def import_image(file):
    img = open_image(file, MAX_REF_IMAGE_SIZE)
    # TODO: error check
    height, width = img.shape

    metadata = extract_metadata(file)

    kp, des = detectAndComputeDescriptors(img)

    # Save the thumbnail
    thumbnail = StringIO.StringIO()
    generate_thumbnail(file, thumbnail, THUMBNAIL_SIZE)
    #thumbnail.write(np.array(cv2.imencode(".jpg", img)[1]).tostring())
    thumbnail.seek(0)

    # Store the description of the image in the DB
    converted_kp = [[p.pt[0], p.pt[1], p.size] for p in kp]
    # Needed for BRISK converted_des = [d.tostring() for d in des]
    ref_image = ReferenceImage(keypoints=converted_kp,
                               descriptors=des,
                               width=width, height=height,
                               metadata=metadata)
    ref_image.thumbnail.put(thumbnail, content_type='image/jpeg')

    ref_image.save()

    # Keep the important bits in memory
    ocv_description = ref_image.to_opencv_description()
    train_matcher(ocv_description, ocv_description[1])

    # ocv_ref_image = [kp, des, ref_image.id, width, height]
    # train_matcher(ocv_ref_image, des)
    return ref_image


def transform_ref_image(mat, w_ref, h_ref):
    pts = np.float32([[0, 0],
                      [0, h_ref - 1],
                      [w_ref - 1, h_ref - 1],
                      [w_ref - 1, 0]])\
            .reshape(-1, 1, 2)
    dst_np = cv2.perspectiveTransform(pts, mat)
    return [o[0].tolist() for o in dst_np]


def normalize(points, w, h):
    def scale_point(pt):
        return [pt[0] / w, pt[1] / h]
    return map(scale_point, points)


def score_transformation(mat, w_ref, h_ref):
    # Transform reference image into image space
    dst = transform_ref_image(mat, w_ref, h_ref)
    print dst

    # Compute vectors
    def diff_vec(ia, ib):
        return [dst[ib][0] - dst[ia][0], dst[ib][1] - dst[ia][1]]
    lines = [diff_vec(1, 0), diff_vec(2, 1), diff_vec(3, 2), diff_vec(0, 3)]

    # First, make sure the points are ordered clockwise or anti-clockwise
    def cross_pdt(u, v):
        return u[0] * v[1] - u[1] * v[0]
    pdts = [cross_pdt(lines[1], lines[0]), cross_pdt(lines[2], lines[1]), cross_pdt(lines[3], lines[2]), cross_pdt(lines[0], lines[3])]

    cur_sign = pdts[0]
    for pdt in pdts:
        if cur_sign * pdt <= 0.0:
            return 0.0, 0.0

    # compute the area of the transformed reference image in the source image
    area = (abs(pdts[0]) + abs(pdts[2])) / 2.0

    # evaluate perspective
    def vec_len(v):
        return math.sqrt(v[0] * v[0] + v[1] * v[1])
    vec_lengths = map(vec_len, lines)
    per_1 = vec_lengths[0] / vec_lengths[2]
    per_2 = vec_lengths[1] / vec_lengths[3]
    if per_1 > 1.0:
        per_1 = 1.0 / per_1
    if per_2 > 1.0:
        per_2 = 1.0 / per_2

    if per_1 < 0.5 or per_2 < 0.5:
        return 0.0, 0.0

    # score the transformation: it's the "rectangularity" of the transformed reference image
    sine = [pdts[0] / (vec_lengths[0] * vec_lengths[1]), pdts[1] / (vec_lengths[1] * vec_lengths[2]), pdts[2] / (vec_lengths[2] * vec_lengths[3]), pdts[3] / (vec_lengths[3] * vec_lengths[0])]
    score = sum(sine) / len(sine)

    return score, area


@timeit
def match_images(kp_img, des_img, max_number_of_matches, min_score=0.0):
    matches = __matcher__.knnMatch(des_img, k=2)

    # Filter matches which are more than 3 times further than the min
    #print matches
    #min_dist = min(matches, key=lambda x:x[0].distance)
    #threshold_dist = 3 * min_dist
    #good_matches = filter(lambda x:x[0].distance <= threshold_dist, matches)

    # Go through the list and group by reference image matched
    grouped_matches = {}

    def append_match(m):
        if m.imgIdx in grouped_matches:
            grouped_matches[m.imgIdx].append(m)
        else:
            grouped_matches[m.imgIdx] = [m]

    for m in matches:
        l = len(m)
        if l >= 2:
            a, b = m
            if a.distance < 0.7 * b.distance:
                append_match(a)
                append_match(b)
        elif l == 1:
            append_match(m[0])

    found_matches = []

    # Iterate over each reference image
    for k, ref_matches in grouped_matches.iteritems():
        if len(ref_matches) > MIN_MATCH_COUNT:
            ref_image = dbcache.get(k)
            mat = find_transformation(kp_img, ref_image, ref_matches)

            if mat is not None:
                score, area = score_transformation(mat, ref_image[3], ref_image[4])
                if score >= min_score:
                    found_matches.append((score, area, ref_image[2], mat))

    sorted_matches = sorted(found_matches, key=itemgetter(0), reverse=True)
    sorted_matches = sorted_matches[:max_number_of_matches]

    return sorted_matches

    # return currentId, currentMat, currentArea, currentScore


def find_transformation(kp_img, ref_image, good_matches):
    kp_ref = ref_image[0]

    # Get the keypoints from the matches
    match_kp_img = np.float32([kp_img[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
    match_kp_ref = np.float32([kp_ref[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

    # Find transformation
    mat, mask = cv2.findHomography(match_kp_ref, match_kp_img, cv2.RANSAC, 5.0)
    return mat


@timeit
def process_image(file, max_number_of_results, min_score=0.0):
    img = open_image(file, MAX_MATCH_IMAGE_SIZE)
    # TODO: error check
    img_h, img_w = img.shape

    # find the keypoints and descriptors
    kp, des = detectAndComputeDescriptors(img)

    matches = match_images(kp, des, max_number_of_results, min_score)
    # currentId, currentMat, currentArea, currentScore = match_images(kp, des)

    if len(matches) == 0:
        return {"error": "No result"}, 404
    else:
        def details_to_results(details):
            # Extract details for the list
            currentScore = details[0]
            currentArea = details[1]
            currentId = details[2]
            currentMat = details[3]

            print details
            # Create a structure to be sent back as JSON
            ref_match = ReferenceImage.objects(id=currentId).first()
            transformed = transform_ref_image(currentMat,
                                              ref_match.width,
                                              ref_match.height)
            transformed_normalized = normalize(transformed, img_w, img_h)

            result = ref_match.to_simple_object()
            result['area'] = currentArea
            result['score'] = currentScore
            result['transformed_normalized'] = transformed_normalized

            return result

        result = map(details_to_results, matches)

        return result, 200


# Initialization
#detectors, extractor, matcher = init_opencv()

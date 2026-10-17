import cv2
import numpy as np


img_path = "test.png"   # 放你的截图名字

img = cv2.imread(img_path)

if img is None:
    print("图片读取失败")
    exit()


h, w = img.shape[:2]

print("图片尺寸:", w, h)


hsv = cv2.cvtColor(
    img,
    cv2.COLOR_BGR2HSV
)


# 绿色范围
mask = cv2.inRange(
    hsv,
    np.array([35,60,60]),
    np.array([90,255,255])
)


# 保存检测结果
cv2.imwrite(
    "green_mask.png",
    mask
)


# 找绿色区域

n, labels, stats, centers = cv2.connectedComponentsWithStats(
    mask,
    8
)


print("检测数量:", n-1)


for i in range(1,n):

    x,y,ww,hh,area = stats[i]

    if (
        area > 80
        and ww > 25
        and hh > 12
    ):

        cx,cy = centers[i]

        print(
            "发现按钮:",
            {
                "x":int(cx),
                "y":int(cy),
                "w":ww,
                "h":hh,
                "area":area
            }
        )


        cv2.rectangle(
            img,
            (x,y),
            (x+ww,y+hh),
            (0,0,255),
            3
        )


cv2.imwrite(
    "detect_result.png",
    img
)


print("完成，查看 detect_result.png")
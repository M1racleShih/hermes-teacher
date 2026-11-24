# Bayes lessons · 贝叶斯课堂节选

These excerpts come from two Chinese-language Hermes Teacher sessions with a live model and simulated learners. Both used the same example material on test sensitivity, false positives and natural frequencies. The README shortens the exchanges and translates them for the English edition. The original wording is quoted below.

两节课使用同一份示例材料、真实模型和模拟学习者。摘录保留课堂中的中文用词。学习者指定了希望探测的范围，也可以要求调整计划。

## A · Which people count? / 分母数哪群人？

A asked to focus on the denominator. After answering “I don’t know,” A requested a simpler question about a class of 30 students. This is the reasoning the teacher used in the revised plan.

> **学习者 A**　不知道<br>
> 分母我还分不清。
>
> **教师**　在一个班级中，共有30名学生，其中9人戴眼镜。若从这30名学生中随机选出一人，要计算"选出的人戴眼镜"这一比例，分母应取多少人？
>
> **学习者 A**　A
>
> **教师反馈节选**　「30人」正确。题目指定的被考察群体是30名学生，比例的分母即为该群体总人数，分子才是其中戴眼镜的9人。

**The learner’s revision request / 学习者怎样改计划**

> 请先补条件概率分母，并单独说明自然频数，这两个前置都指向用自然频数求后验。再呈现修改版。

**The teacher’s revised plan / 老师给出的第二版路线**

> 1. 条件概率的分母：把「已知条件」翻译成被考察的人群
> 2. 自然频数：把百分比落地成固定总数下的人数
> 3. 用自然频数求后验：构造阳性者人群并计算患病比例

**The next check / 换个条件再试一次**

> **教师题面节选**　现已知某人检测呈阴性，若要求「阴性者中未患病的比例」，分母应取哪一类人的数量？
>
> **学习者 A 的作答备注**　我用患病且阳性的人数除以所有阳性人数。
>
> **教师续课答复节选**　下次复测会从更简单的一步开始——先确认你能跟着「已知条件」换人群，再回到原题变体，通过后进入节点②自然频数。

The original answer selected the group with the disease and the note referred to positive tests. The Desktop screenshots replay the note using a shorter question and a positive-group option.

## B · How many false positives? / 假阳性的人数怎么算？

B asked to start by checking the meaning of the false-positive rate. After B answered, the teacher chose a question about converting rates to counts. The learner later requested an example with a whole number of hundreds after two follow-up questions failed to generate. The teacher used 200 people.

> **教师题面节选**　请问「假阳性率 5%」指的是哪一类人群中检测呈阳性的比例？
>
> **学习者 B**　看的是没有患病的人：先把人群限定为未患病者，再数其中检测为阳性的人。
>
> **学习者 B，换算人数时**　真阳性大概 10 人。假阳性要在 990 名未患病者里算，可是我不会把其中的 5% 换算成人数。
>
> **学习者 B，换作 200 人时**　200 是两个 100，每 100 人里有 5 人，加起来 10 人。这个可以凑整百，刚才 990 人那题我还不确定怎么算。

**How B asked to practise / B 要求怎样练习**

> 我想每一步都先自己动手算，再看提示和答案。整百的例子我能算，遇到 990 人还是没把握。请按这个节奏调整一下计划，再给我确认。

**From the revised plan / 第二版计划中的练习**

> 2. 非整百换算第一招：990=1000−10 拆开算
> 3. 非整百换算第二招：放大到10000人去掉半个人
> 4. 合并阳性行：先自己除，再看比例

**Checking the numbers / 核对数字**

The first plan gave about 2% in its explanation. The revised plan uses the example’s actual rates. Out of 10,000 people, 100 have the disease and 99 of them test positive. Of the other 9,900, another 495 test positive. That makes **99 out of 594 positives**, or **1/6 (about 16.7%)**.

第一版的计划说明曾把本题结果写成约 2%，第二版已改正。示例中的患病率 1% 是全体人群中患病者的比例。按 10,000 人计算，100 名患者中 99 人检测阳性，9,900 名未患病者中 495 人检测阳性，所以阳性者中患病的比例是 **99 ÷ (99 + 495) = 1/6，约 16.7%**。

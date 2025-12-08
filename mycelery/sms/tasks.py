# tasks.py

from CheckObjectionApp.models import TestCase, topic, Contest, ContestTopic, ContestParticipant, ContestSubmission
from CheckObjectionApp.utils.judge0_service import Judge0Service
from mycelery.main import app
import time
import logging
import json
import requests
from celery import shared_task
from django.core.cache import cache
from django.utils import timezone
from django.db import transaction

logging.getLogger("django")


@app.task
def send_sms(mobile):
    print(f"向手机号%s发送短信成功！" % mobile)
    time.sleep(5)
    return "send_sms OK"


@app.task
def send_sms2(mobile):
    print("向手机号%s发送短信成功！" % mobile)
    time.sleep(5)
    return "send_sms2 OK"

# todo 给比赛提交代码逻辑添加有关信息
@shared_task(bind=True, max_retries=3)
def submit_code_task(self, submission_data):
    """
    异步提交代码到Judge0进行判题 - 支持普通提交和比赛提交
    """
    try:
        contest_id = submission_data.get('contest_id', None)
        source_code = submission_data.get('source_code', '')
        language_id = submission_data.get('language_id', 71)
        topic_id = submission_data.get('topic_id', '')
        user_name = submission_data.get('user_name', '')
        user_id = submission_data.get('user_id', None)
        submission_id = submission_data.get('submission_id', '')
        notes = submission_data.get('notes', '')

        # 根据题目ID获取测试用例（使用缓存）
        test_cases = get_test_cases_by_topic_with_cache(topic_id)

        judge_service = Judge0Service()
        results = []

        # 对每个测试用例进行判题
        for i, test_case in enumerate(test_cases):
            result = judge_service.submit_code(
                source_code=source_code,
                language_id=language_id,
                stdin=test_case["stdin"],
                expected_output=test_case["expected_output"]
            )

            # 构建详细的测试结果
            detailed_result = {
                "test_case": i + 1,
                "input_data": test_case["stdin"],
                "expected_output": test_case["expected_output"],
                "user_output": result.get("stdout", ""),
                "result": result,
                "is_sample": test_case.get("is_sample", False),
                "score": test_case.get("score", 0)
            }
            results.append(detailed_result)

        # 计算总体结果
        overall_result = calculate_overall_result(results)

        # 保存提交结果到数据库
        submission = save_submission_result(
            submission_id=submission_id,
            user_name=user_name,
            user_id=user_id,
            topic_id=topic_id,
            source_code=source_code,
            language_id=language_id,
            results=results,
            notes=notes,
            overall_result=overall_result,
        )

        # 如果是比赛提交，保存比赛相关记录
        if contest_id:
            save_contest_submission_result(
                contest_id=contest_id,
                submission=submission,
                user_id=user_id,
                topic_id=topic_id,
                overall_result=overall_result,
                results=results
            )

        # 返回结果
        return {
            "success": True,
            "overall_result": overall_result,
            "results": results,
            "user_name": user_name,
            "topic_id": topic_id,
            "submission_id": submission_id,
            "source_code": source_code,
            "language_id": language_id,
            "contest_id": contest_id
        }

    except Exception as e:
        # 如果发生异常，重试
        raise self.retry(countdown=2 ** self.request.retries, exc=e)


@shared_task
def process_test_run(submission_data):
    """
    处理测试运行（快速返回，不保存结果）
    """
    try:
        source_code = submission_data.get('source_code', '')
        language_id = submission_data.get('language_id', 71)
        topic_id = submission_data.get('topic_id', '')

        # 只获取样例测试用例
        test_cases = get_sample_test_cases(topic_id)

        judge_service = Judge0Service()
        results = []

        # 对每个样例测试用例进行判题
        for i, test_case in enumerate(test_cases):
            result = judge_service.submit_code(
                source_code=source_code,
                language_id=language_id,
                stdin=test_case["stdin"],
                expected_output=test_case["expected_output"]
            )

            detailed_result = {
                "test_case": i + 1,
                "input_data": test_case["stdin"],
                "expected_output": test_case["expected_output"],
                "user_output": result.get("stdout", ""),
                "result": result,
                "is_sample": test_case.get("is_sample", False),
                "score": test_case.get("score", 0)
            }
            results.append(detailed_result)

        overall_result = calculate_overall_result(results)

        return {
            "success": True,
            "overall_result": overall_result,
            "results": results,
            "is_test_run": True
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"测试运行失败: {str(e)}",
            "is_test_run": True
        }


@shared_task
def process_contest_submission(contest_id, submission_id, user_id, topic_id):
    """
    专门处理比赛提交的任务
    """
    try:
        # 获取提交记录
        from CheckObjectionApp.models import Submission
        submission = Submission.objects.get(id=submission_id)

        # 计算比赛相关数据
        update_contest_ranking(contest_id, user_id, topic_id, submission.overall_result)

        return {
            "success": True,
            "contest_id": contest_id,
            "submission_id": submission_id,
            "message": "比赛提交处理完成"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"比赛提交处理失败: {str(e)}"
        }


def save_submission_result(submission_id, user_name, user_id, topic_id, source_code,
                           language_id, results, overall_result, notes):
    """
    保存提交结果到数据库
    """
    from CheckObjectionApp.models import Submission
    from django.contrib.auth.models import User

    try:
        user = None
        if user_id:
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                pass

        submission = Submission.objects.create(
            id=submission_id,
            user=user,
            user_name=user_name,
            topic_id=topic_id,
            source_code=source_code,
            language_id=language_id,
            results=results,
            overall_result=overall_result,
            notes=notes,
            status='completed'
        )
        return submission
    except Exception as e:
        print(f"保存提交结果失败: {str(e)}")
        return None


def save_contest_submission_result(contest_id, submission, user_id, topic_id, overall_result, results):
    """
    保存比赛提交相关记录
    """
    try:
        from django.contrib.auth.models import User

        # 获取比赛和参与者
        contest = Contest.objects.get(id=contest_id)
        user = User.objects.get(id=user_id)
        participant = ContestParticipant.objects.get(contest=contest, user=user)

        # 创建比赛提交记录
        contest_submission = ContestSubmission.objects.create(
            contest=contest,
            submission=submission,
            participant=participant
        )

        # 异步更新比赛排名
        process_contest_submission.delay(contest_id, submission.id, user_id, topic_id)

        return contest_submission
    except Exception as e:
        print(f"保存比赛提交记录失败: {str(e)}")
        return None


def update_contest_ranking(contest_id, user_id, topic_id, overall_result):
    """
    更新比赛排名数据（这里可以扩展为更复杂的排名逻辑）
    """
    try:
        # 这里可以实现比赛排名逻辑
        # 包括：计算得分、罚时、排名等
        print(f"更新比赛 {contest_id} 中用户 {user_id} 的排名")

        # 示例逻辑：可以根据需要扩展
        if overall_result == "Accepted":
            print(f"用户 {user_id} 在题目 {topic_id} 上通过了测试")

        return True
    except Exception as e:
        print(f"更新比赛排名失败: {str(e)}")
        return False


def get_test_cases_by_topic_with_cache(topic_id):
    """
    根据题目ID从Redis缓存或数据库获取测试用例
    """
    try:
        test_cases = TestCase.objects.filter(
            titleSlug_id=topic_id
        ).order_by('order')

        formatted_test_cases = []
        for test_case in test_cases:
            formatted_test_cases.append({
                "stdin": test_case.input_data,
                "expected_output": test_case.expected_output,
                "is_sample": test_case.is_sample,
                "score": test_case.score
            })

        return formatted_test_cases

    except Exception as e:
        return [{
            "stdin": "",
            "expected_output": "",
            "is_sample": False,
            "score": 0
        }]


def get_sample_test_cases(topic_id):
    """
    获取样例测试用例
    """
    try:
        test_cases = TestCase.objects.filter(
            titleSlug_id=topic_id,
            is_sample=True
        ).order_by('order')

        formatted_test_cases = []
        for test_case in test_cases:
            formatted_test_cases.append({
                "stdin": test_case.input_data,
                "expected_output": test_case.expected_output,
                "is_sample": test_case.is_sample,
                "score": test_case.score
            })

        return formatted_test_cases

    except Exception as e:
        return [{
            "stdin": "",
            "expected_output": "",
            "is_sample": True,
            "score": 0
        }]


def calculate_overall_result(results):
    """
    计算总体判题结果
    """
    if not results:
        return "No Test Cases"

    # 检查所有测试用例是否都通过
    all_passed = all(
        result["result"].get("status") == "Accepted"
        for result in results
    )

    if all_passed:
        return "Accepted"
    else:
        # 返回第一个失败的结果状态
        for result in results:
            if result["result"].get("status") != "Accepted":
                return result["result"].get("status", "Wrong Answer")
        return "Wrong Answer"


@shared_task
def validate_contest_submission(contest_id, user_id, topic_id):
    """
    验证比赛提交的合法性
    """
    try:
        from django.utils import timezone

        contest = Contest.objects.get(id=contest_id)
        current_time = timezone.now()

        # 检查比赛时间
        if current_time < contest.start_time:
            return {
                "valid": False,
                "error": "比赛尚未开始"
            }

        if current_time > contest.end_time:
            return {
                "valid": False,
                "error": "比赛已结束"
            }

        # 检查用户是否报名参赛
        try:
            ContestParticipant.objects.get(contest=contest, user_id=user_id)
        except ContestParticipant.DoesNotExist:
            return {
                "valid": False,
                "error": "未报名参加该比赛"
            }

        # 检查题目是否属于比赛
        try:
            ContestTopic.objects.get(contest=contest, topic_id=topic_id)
        except ContestTopic.DoesNotExist:
            return {
                "valid": False,
                "error": "该题目不属于本比赛"
            }

        return {
            "valid": True,
            "message": "提交验证通过"
        }

    except Exception as e:
        return {
            "valid": False,
            "error": f"验证失败: {str(e)}"
        }
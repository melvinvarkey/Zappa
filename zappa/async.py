"""
Zappa Async Tasks

Example:
```
from zappa.async import task

@task(service='sns')
def my_async_func(*args, **kwargs):
    dosomething()
```

For SNS, you can also pass an `arn` argument to task() which will specify which SNS path to send it to.

Without `service='sns'`, the default service is 'lambda' which will call the method in an asynchronous
lambda call.

The following restrictions apply:
* function must have a clean import path -- i.e. no closures, lambdas, or methods.
* args and kwargs must be JSON-serializable.
* The JSON-serialized form must be within the size limits for Lambda (128K) or SNS (256K) events.

Discussion of this comes from:
    https://github.com/Miserlou/Zappa/issues/61
    https://github.com/Miserlou/Zappa/issues/603
    https://github.com/Miserlou/Zappa/pull/694
    https://github.com/Miserlou/Zappa/pull/732

"""

import os
import json
import importlib
import inspect
import boto3
import botocore

from util import get_topic_name

AWS_REGION = os.environ.get('AWS_REGION')
AWS_LAMBDA_FUNCTION_NAME = os.environ.get('AWS_LAMBDA_FUNCTION_NAME')

# Declare these here so they're kept warm.
try:
    LAMBDA_CLIENT = boto3.client('lambda')
    SNS_CLIENT = boto3.client('sns')
    STS_CLIENT = boto3.client('sts')
except botocore.exceptions.NoRegionError as e:
    # This can happen while testing on Travis, but it's taken care  of
    # during class initialization.
    pass


##
# Response and Exception classes
##

class AsyncException(Exception):
    """ Simple exception class for async tasks. """
    pass

class LambdaAsyncResponse(object):
    """
    Base Response Dispatcher class
    Can be used directly or subclassed if the method to send the message is changed.
    """
    def __init__(self, **kwargs):
        """ """
        if kwargs.get('boto_session'):
            self.client = kwargs.get('boto_session').client('lambda')
        else:
            self.client = LAMBDA_CLIENT

    def send(self, task_path, args, kwargs):
        """
        Create the message object and pass it to the actual sender.
        """
        message = {
                'task_path': task_path,
                'args': args,
                'kwargs': kwargs
            }
        self._send(message)
        return self

    def _send(self, message):
        """
        Given a message, directly invoke the lamdba function for this task.
        """
        message['command'] = 'zappa.async.route_lambda_task'
        payload = json.dumps(message).encode('utf-8')
        if len(payload) > 128000:
            raise AsyncException("Payload too large for async Lambda call")
        self.response = self.client.invoke(
                                    FunctionName=AWS_LAMBDA_FUNCTION_NAME,
                                    InvocationType='Event', #makes the call async
                                    Payload=payload
                                )
        self.sent = (self.response.get('StatusCode', 0) == 202)

class SnsAsyncResponse(LambdaAsyncResponse):
    """
    Send a SNS message to a specified SNS topic
    Serialise the func path and arguments
    """
    def __init__(self, **kwargs):

        if kwargs.get('boto_session'):
            self.client = kwargs.get('boto_session').client('sns')
        else:
            self.client = SNS_CLIENT

        if kwargs.get('arn'):
            self.arn = kwargs.get('arn')
        else:
            if kwargs.get('boto_session'):
                sts_client = kwargs.get('boto_session').client('sts')
            else:
                sts_client = STS_CLIENT
            AWS_ACCOUNT_ID = sts_client.get_caller_identity()['Account']
            self.arn = 'arn:aws:sns:{region}:{account}:{topic_name}'.format(
                                    region=AWS_REGION,
                                    account=AWS_ACCOUNT_ID,
                                    topic_name=get_topic_name(AWS_LAMBDA_FUNCTION_NAME)
                                )

    def _send(self, message):
        """
        Given a message, publish to this topic.
        """
        payload = json.dumps(message)
        if len(payload) > 256000:
            raise AsyncException("Payload too large for SNS")
        self.response = self.client.publish(
                                TargetArn=self.arn,
                                Message=payload
                            )
        self.sent = self.response.get('MessageId')

##
# Aync routers and utility functions
##

ASYNC_CLASSES = {
    'lambda': LambdaAsyncResponse,
    'sns': SnsAsyncResponse,
}

def _import_and_get_task(task_path):
    """
    Given a modular path to a function, import that module
    and return the function.
    """
    module, function = task_path.rsplit('.', 1)
    app_module = importlib.import_module(module)
    app_function = getattr(app_module, function)
    return app_function


def _get_func_task_path(func):
    """
    Format the modular task path for a function via inspection.
    """
    module_path = inspect.getmodule(func).__name__
    task_path = '{module_path}.{func_name}'.format(
                                        module_path=module_path,
                                        func_name=func.__name__
                                    )
    return task_path


def route_lambda_task(event, context):
    """
    Deserialises the message from event passed to zappa.handler.run_function
    imports the function, calls the function with args
    """
    message = event
    func = _import_and_get_task(message['task_path'])
    return func(
            *message['args'],
            **message['kwargs']
        )


def route_sns_task(event, context):
    """
    Gets SNS Message, deserialises the message,
    imports the function, calls the function with args
    """
    record = event['Records'][0]
    message = json.loads(
            record['Sns']['Message']
        )
    func = _import_and_get_task(message['task_path'])
    return func(
            *message['args'],
            **message['kwargs']
        )


def run(func, args=[], kwargs={}, service='lambda', **task_kwargs):
    """
    Instead of decorating a function with @task, you can just run it directly.
    If you were going to do func(*args, **kwargs), then you will call this:
    import zappa.async.run
    zappa.async.run(func, args, kwargs)
    If you want to use SNS, then do:
    zappa.async.run(func, args, kwargs, service='sns')
    and other arguments are similar to @task
    """
    task_path = _get_func_task_path(func)
    return ASYNC_CLASSES[service](**task_kwargs).send(task_path, args, kwargs)


def task(service='lambda', **task_kwargs):
    """
    Async task decorator for a function.
    Serialises and dispatches the task to SNS.
    Lambda subscribes to SNS topic and gets this message
    Lambda routes the message to the same function
    """
    def _invoker(func, task_path):
        """ """
        def _invoker_inner(*args, **kwargs):
            """ """
            if service in ASYNC_CLASSES:
                return ASYNC_CLASSES[service](**task_kwargs).send(task_path, args, kwargs)
            return func(*args, **kwargs)
        return _invoker_inner

    def _wrap(func):
        """ """
        task_path = _get_func_task_path(func)
        func.async = _invoker(func, task_path)
        return func

    return _wrap

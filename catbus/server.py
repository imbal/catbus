"""catbus.server
a http server that routes requests to the
mapped objects, handling transforming them
into rson wire objects
"""
import threading
import types
import socket
import traceback
import sys
import inspect
import uuid

from urllib.parse import urljoin, urlencode
from wsgiref.simple_server import make_server, WSGIRequestHandler

from werkzeug.utils import redirect as Redirect
from werkzeug.wrappers import Request, Response
from werkzeug.exceptions import HTTPException

from . import dom

def funcargs(m):
    args =  m.__code__.co_varnames[:m.__code__.co_argcount]
    args = [a for a in args if not a.startswith('_')]
    if args and args[0] == 'self': args.pop(0)
    return args

def make_resource(obj, url):
    cls = obj.__class__

    links, actions = extract_actions(cls)
    attributes = extract_attributes(obj)

    metadata = dict(
        url = url,
        links = links,
        actions = actions,
    )

    return dom.Resource(
        kind = cls.__name__,
        metadata = metadata,
        attributes = attributes,
    )

def extract_actions(cls):
    links = []
    all_methods = getattr(cls, 'rpc', False)
    actions = dict()
    for k,v in cls.__dict__.items():
        if not getattr(v, 'rpc', all_methods): continue
        if k.startswith('_'): continue
        if isinstance(v, type) or not isinstance(v, types.FunctionType): continue

        if getattr(v, 'safe', False):
            links.append(k)
        else:
            actions[k] = funcargs(v)
    return links, actions

def extract_attributes(obj):
    attributes = dict()
    for k,v in obj.__dict__.items():
        if k.startswith('_'): continue
        
        attributes[k]= v
    return attributes



def rpc(safe=False):
    def _fn(fn):
        fn.rpc = True
        fn.safe = safe
        return fn
    return _fn

def waiter():
    def _fn(fn):
        fn.rpc = True
        fn.waiter = None
        def _wait():
            def _decorator(wait_fn):
                fn.waiter = wait_fn
                return fn
            return _decorator
        fn.ready = _wait
        return fn
    return _fn


class Embed:
    pass

class Nesting:
    pass


class RequestHandler:
    def subtypes(self):
        return ()

    def inline(self, prefix):
        return None

    def invoke(self, obj, args=None, params=None, safe=False):
        if not safe:
            if args:
                return obj(**args)
            else:
                return obj()
        else:
            if not obj.safe:
                raise dom.MethodNotAllowed()
            return obj()

    def invoke_waiter(self, waiter, obj, params):
        params = {key: dom.parse(value) for key,value in params.items()}
        # if waiter is a fn
        if obj is None:
            out = waiter(**params)
        else:
            out = waiter(obj, **params)

        # if waiter is a waiter, call.resolve()

        if isinstance(out, Waiter):
            out.from_resolve = True
        return out

class NestedHandler(RequestHandler):
    def __init__(self, name, cls):
        self.name = name
        self.cls = cls

        self.for_path = {}
        self.for_type = {}
        self.add_nested_handlers()

    def add_nested_handlers(self):

        pass

    def subpath(self, path):
        return path.split('/',1)[0]

    def on_request(self, context, request):
        path = request.url

        if not path.startswith(self.name):
            raise dom.NotFound()

        path = path[len(self.name)+1:]

        subpath = self.subpath(path)

        if subpath in self.for_path:
            r = dom.Request(
                    request.method,
                    path, request.params,
                    request.headers,
                    request.data,
            )
            return self.nested_request(subpath, context, r)

        elif subpath:
            raise dom.NotFound()
        else:
            return self.handle_request(context, request)

    def handle_request(context, request):
        pass

    def nested_request(self, subpath, context, request):
        return self.for_path[subpath].on_request(context, request)

    def add_nested_handler(self, name, cls, handler):
        if name in self.for_path or cls in self.for_type:
            raise Exception('dupe')
        self.for_path[name] = handler
        self.for_type[cls] = handler
        for t in handler.subtypes():
            if t in self.for_type:
                raise Exception('dupe')
            self.for_type[t] = handler

    def subtypes(self):
        return self.for_type.keys()

    def embed(self, prefix, o):
        sub_prefix = "{}/".format(self.url(prefix))
        if o in self.for_type:
            return self.for_type[o].embed(sub_prefix, o)
        elif hasattr(o, '__self__') and o.__self__ in self.for_type:
            return self.for_type[o.__self__].embed(sub_prefix, o)
        elif hasattr(o, '__class__') and o.__class__ in self.for_type:
            return self.for_type[o.__class__].embed(sub_prefix, o)
        else:
            return self.handle_embed(prefix, o)

    def handle_embed(self, prefix, obj):
        pass

    
class Waiter(Embed):
    suffix = '/wait'

    def __init__(self, **args):
        self.args = args
        self.from_resolve = False

    def embed(self, prefix, name):
        params = {key: dom.dump(value) for key, value in self.args.items()}
        if not self.from_resolve:
            name = "{}{}".format(name,self.suffix) 
        url = "{}{}?{}".format(prefix, name,urlencode(params))
        metadata = dict()
        metadata["url"] = url
        metadata["wait_seconds"] = 2
        return dom.Waiter(
            metadata = metadata,
        )

class FunctionHandler(RequestHandler):
    def __init__(self, name, function):
        self.fn = function
        self.name = name

    def on_request(self, context, request):
        method, path, params, data = request.method, request.url, request.params, request.data
        path = path[len(self.name)+1:]
        if path == 'wait':
            if method == 'GET':
                return self.invoke_waiter(self.fn.waiter, None, params)
            else:
                return MethodNotAllowed()
        elif path:
            raise dom.NotFound()

        if method == 'GET':
            return self.invoke(self.fn, safe=True)
        elif method == 'POST':
            return self.invoke(self.fn, args=data)
        raise dom.MethodNotAllowed()

    def url(self, prefix):
        return prefix+self.name

    def link(self, prefix):
        if getattr(self.fn, 'safe', False):
            return dom.Link(self.url(prefix))
        else:
            return dom.Form(self.url(prefix), arguments=funcargs(self.fn))

    def embed(self, prefix, o):
        if o is None or o is self.fn:
            return self.link(prefix)
        else:
            Exception('bad embed')

class MethodHandler(RequestHandler):
    def __init__(self, name, cls_name, method):
        self.cls_name = cls_name
        self.name = name
        self.method = method

    def on_request(self, context, request):
        method, path, params, data = request.method, request.url, request.params, request.data
        
        obj = context[self.cls_name]
        fn = getattr(obj, self.name)

        path = path[len(self.name)+1:]
        if path == 'wait':
            if method == 'GET':
                return self.invoke_waiter(fn.waiter, None, params)
            else:
                return MethodNotAllowed()
        elif path:
            raise dom.NotFound()

        if method == 'GET':
            return self.invoke(fn, safe=True)
        elif method == 'POST':
            return self.invoke(fn, args=data)
        raise dom.MethodNotAllowed()

    def url(self, prefix):
        return prefix+self.name

    def link(self, prefix):
        if getattr(self.method, 'safe', False):
            return dom.Link(self.url(prefix))
        else:
            return dom.Form(self.url(prefix), arguments=funcargs(self.method))
        
class Namespace:
    rpc = True
    
    def __init__(self):
        pass

    class Handler(NestedHandler):
        def __init__(self, name, cls):
            NestedHandler.__init__(self, name, cls)
            self.obj = self.cls()

        def add_nested_handlers(self):
            for name, method in self.cls.__dict__.items():
                if isinstance(method, type) and hasattr(method, 'Handler'):
                    handler = method.Handler(name, method)
                    self.add_nested_handler(name, method, handler)
                elif isinstance(method, types.FunctionType):
                    handler = FunctionHandler(self.name, method)
                    self.add_nested_handler(name, method, handler)

        def subpath(self, path):
            return path.split('/',1)[0]

        def handle_request(self, context, request):
            if request.method == 'GET':
                return self.obj
            else:
                raise dom.MethodNotAllowed()
        
        def nested_request(self, path, context, request):
            context[self.name] = self.obj
            return NestedHandler.nested_request(self, path, context, request)

        def url(self, prefix):
            return prefix + self.name

        def link(self, prefix):
            return dom.Link(self.url(prefix))

        def inline(self, prefix):
            return self.handle_embed(prefix, self.obj)

        def handle_embed(self,prefix, o):
            sub_prefix = "{}/".format(self.url(prefix))
            if o is None or o is self.cls:
                return self.link(prefix)
            elif not o is self.obj:
                raise Exception('bad handler')

            links, actions = extract_actions(self.cls)
            attributes = extract_attributes(self.obj)
            embeds = {}
            for name, handler in self.for_path.items():
                if name in links: continue
                if name in actions: continue
                inline = handler.inline(sub_prefix)
                if inline: embeds[name] = inline
                links.append(name)

            metadata = dict(
                url = self.url(prefix),
                links = links,
                actions = actions,
                embeds = embeds,
            )

            return dom.Namespace(
                kind = self.cls.__name__,
                metadata = metadata,
                attributes = attributes,
            )

class Service:
    rpc = True

    def __init__(self):
        pass

    def __getattribute__(self, name):
        if name.startswith('_'):
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, '__class__'),name)

    class Handler(NestedHandler):
        def add_nested_handlers(self):
            for name, method in self.cls.__dict__.items():
                if isinstance(method, type) and hasattr(method, 'Handler'):
                    handler = method.Handler(name, method)
                    self.add_nested_handler(name, method, handler)
                elif isinstance(method, types.FunctionType):
                    handler = FunctionHandler(name, method)
                    self.add_nested_handler(name, method, handler)

        def handle_request(self, context, request):
            if request.method == 'GET':
                return self.cls()
            else:
                raise dom.MethodNotAllowed()

        def url(self, prefix):
            return prefix+self.name

        def link(self, prefix):
            return dom.Link(self.url(prefix))

        def inline(self, prefix):
            return self.handle_embed(prefix, self.cls())

        def handle_embed(self,prefix, o):
            sub_prefix = "{}/".format(self.url(prefix))
            if o is None or o is self.cls:
                return self.link(prefix)

            links, actions = extract_actions(self.cls)
            attributes = {}
            embeds = {}
            for name, handler in self.for_path.items():
                if name in links: continue
                if name in actions: continue
                inline = handler.inline(sub_prefix)
                if inline: embeds[name] = inline
                links.append(name)

            metadata = dict(
                url = self.url(prefix),
                links = links,
                actions = actions,
                embeds = embeds,
            )

            return dom.Namespace(
                kind = self.cls.__name__,
                metadata = metadata,
                attributes = attributes,
            )

class Singleton:
    rpc = True

    def __init__(self):
        pass

    class Handler(NestedHandler):
        def __init__(self, name, cls):
            NestedHandler.__init__(self, name, cls)
            self.obj = self.cls()

        def add_nested_handlers(self):
            for name, method in self.cls.__dict__.items():
                if isinstance(method, type) and hasattr(method, 'Handler'):
                    handler = method.Handler(name, method)
                    self.add_nested_handler(name, method, handler)
                elif isinstance(method, types.FunctionType):
                    handler = MethodHandler(name, self.name, method)
                    self.add_nested_handler(name, method, handler)

        def handle_request(self, context, request):
            if request.method == 'GET':
                return self.obj
            else:
                raise dom.MethodNotAllowed()
        
        def nested_request(self, path, context, request):
            context[self.name] = self.obj
            return NestedHandler.nested_request(self, path, context, request)

        def url(self, prefix):
            return prefix + self.name

        def link(self, prefix):
            return dom.Link(self.url(prefix))

        def inline(self, prefix):
            return self.handle_embed(prefix, self.obj)

        def handle_embed(self,prefix, o):
            sub_prefix = "{}/".format(self.url(prefix))
            if o is None or o is self.cls:
                return self.link(prefix)
            elif not o is self.obj:
                raise Exception('bad handler')

            links, actions = extract_actions(self.cls)
            attributes = extract_attributes(self.obj)
            embeds = {}
            for name, handler in self.for_path.items():
                if name in links: continue
                if name in actions: continue
                inline = handler.inline(sub_prefix)
                if inline: embeds[name] = inline
                links.append(name)

            metadata = dict(
                url = self.url(prefix),
                links = links,
                actions = actions,
                embeds = embeds,
            )

            return dom.Namespace(
                kind = self.cls.__name__,
                metadata = metadata,
                attributes = attributes,
            )

class Token:
    rpc = True
    class Handler(RequestHandler):
        def __init__(self, name, view):
            self.view = view
            self.name = name

        def on_request(self, context, request):
            method, path, params, data = request.method, request.url, request.params, request.data
            path = path[len(self.name)+1:]
            if path.startswith('_'): 
                raise dom.Forbidden()
            if '/' in path:
                raise dom.NotFound()

            if params:
                obj =  self.lookup(params)

                if not path:
                    if method == 'GET':
                        return obj
                    raise dom.MethodNotAllowed()

                fn = getattr(obj, path)

                if method == 'GET':
                    return self.invoke(fn, safe=True)
                elif method == 'POST':
                    return self.invoke(fn, args=data)
                else:
                    raise dom.MethodNotAllowed()
            else:
                if path:
                    raise dom.NotImplemented()

                if method == 'GET':
                    return self.view
                elif method == 'POST':
                    return self.view(**data)
                raise dom.MethodNotAllowed()

        def lookup(self, params):
            params = {key: dom.parse(value) for key,value in params.items() if not key.startswith('_')}
            obj = self.view(**params)
            return obj

        def url(self, prefix):
            return prefix + self.name

        def link(self, prefix):
            args = funcargs(self.view.__init__)
            return dom.Form(self.url(prefix), arguments=args)

        def embed(self, prefix, o):
            if o is None or o is self.view:
                return self.link(prefix)
            params = {key: dom.dump(value) for key, value in o.__dict__.items()}
            url = "{}?{}".format(self.url(prefix), urlencode(params))

            return make_resource(o, url)

class Collection:
    class List(Embed):
        suffix = '/list'
        def __init__(self, name, items, selector, next):
            self.name = name
            self.items = items
            self.selector = selector
            self.next = next

        def embed(self, prefix, name):
            metadata = dict()
            metadata["collection"] = "{}{}{}".format(prefix, self.name, self.suffix)
            metadata["selector"] = self.selector
            metadata["continue"] = self.next

            return dom.Cursor(
                kind = self.name,
                items = self.items,
                metadata = metadata,
            )
    def dict_handler(name, d=None):
        if d is None:
            d = dict()
        class Handler(Collection.Handler):
            items = d
            key = name

            def key_for(self, obj):
                return getattr(obj, self.key)

            def lookup(self, name):
                return self.items[name]

            def create(self, data):
                name = data[self.key]
                j = self.items[name] = self.cls(**data)
                return j

            def delete(self, name):
                self.items.pop(name)

            def list(self, selector, limit, next):
                return Collection.List(
                    name=self.name, 
                    items=list(self.items.values()),
                    selector=selector,
                    next=None,
                )


        return Handler

    class Handler(RequestHandler):
        def __init__(self, name, cls):
            self.cls = cls
            self.name = name

        def on_request(self, context, request):
            method, path, params, data = request.method, request.url, request.params, request.data
            col_method, path = path[len(self.name)+1:], None

            if '/' in col_method:
                col_method, path = col_method.split('/',1)

            if col_method =='id':
                if '/' in path:
                    id, obj_method = path.split('/',1)
                else:
                    id, obj_method = path, None

                if obj_method and obj_method.startswith('_'):
                    raise dom.Forbidden()
                
                if not obj_method:
                    if method == 'GET':
                        return self.lookup(id)
                    elif method == 'DELETE':
                        self.delete(id)
                        return None
                    else:
                        raise dom.MethodNotAllowed()
                elif '/' in obj_method:
                    obj_method, subpath = obj_method.split('/',1)

                    obj = self.lookup(id)
                    fn = getattr(obj, obj_method)

                    if subpath == 'wait':
                        if method != 'GET':
                            raise MethodNotAllowed()
                        return self.invoke_waiter(fn.waiter, obj,  params)
                    else:
                        raise dom.NotFound()
                else:
                    obj = self.lookup(id)
                    fn = getattr(obj, obj_method)

                    if method == 'GET':
                        return self.invoke(fn, params=params, safe=True)
                    elif method == 'POST':
                        return self.invoke(fn, data)
                    else:
                        raise dom.MethodNotAllowed()

            elif col_method =='list':
                if method == 'GET':
                    selector = params.get('where',None)
                    limit = params.get('limit')
                    next = params.get('continue')
                    if limit:
                        limit = int(limit)
                    selector = dom.parse_selector(selector)
                    return self.list(selector, limit, next)
                elif method == 'DELETE':
                    selector = params['where']
                    selector = dom.parse_selector(selector)
                    self.delete_list(selector)
                    return
                else:
                    raise dom.MethodNotAllowed()
            elif col_method == 'new':
                if method != 'POST':
                    raise dom.MethodNotAllowed()
                return self.create(data)
            elif col_method == 'delete':
                if method != 'POST':
                    raise dom.MethodNotAllowed()
                return self.delete(path)
            elif col_method == '':
                if method != 'GET':
                    raise dom.MethodNotAllowed()
                return self.cls

            raise dom.NotImplelmented(method)

        def url(self, prefix):
            return prefix+self.name

        def link(self, prefix):
            metadata = dict(
                url = self.url(prefix),
                new=self.create_args(),
                list=self.selector_args(),
                key=self.key
            )
            return dom.Dataset(
                kind=self.cls.__name__,
                metadata = metadata
            )
                    
        def create_args(self):
            return funcargs(self.cls.__init__)

        def selector_args(self):
            return ()

        def embed(self, prefix, o):
            if o is None or o is self.cls:
                return self.link(prefix)

            url = self.url_for(prefix, o)

            links, actions = self.extract_actions(self.cls)

            attributes = self.extract_attributes(o)

            metadata = dict(
                id = self.key_for(o),
                collection = self.url(prefix),
                url = url,
                links = links,
                actions = actions,
            )

            return dom.Resource(
                kind = self.cls.__name__,
                metadata = metadata,
                attributes = attributes,
            )

        def extract_actions(self, obj):
            return extract_actions(obj)

        def extract_attributes(self, obj):
            return extract_attributes(obj)

        def url_for(self, prefix, o):
            return "{}{}/id/{}".format(prefix,self.name,self.key_for(o))

        # override

        def key_for(self, obj):
            raise Exception('unimplemented')

        def lookup(self, key):
            raise Exception('unimplemented')

        def create(self, data):
            raise Exception('unimplemented')

        def delete(self, name):
            raise Exception('unimplemented')

        def delete_list(self, selector):
            raise Exception('unimplemented')

        def list(self, selector, limit, next):
            raise Exception('unimplemented')

        def watch(self, selector):
            raise Exception('unimplemented')

class Model:
    class PeeweeHandler(Collection.Handler):
        def __init__(self, name, cls):
            self.pk = cls._meta.primary_key
            self.key = self.pk.name
            self.fields = cls._meta.fields
            self.create_fields = list(k for k,v in self.fields.items() if not v.primary_key)
            self.indexes = [self.pk.name]
            self.indexes.extend(k for k,v in self.fields.items() if v.index or v.unique) 
            Collection.Handler.__init__(self, name, cls)


        def create_args(self):
            return self.create_fields

        def selector_args(self):
            return self.indexes

        def extract_attributes(self, obj):
            attr = dict()
            for name in self.fields:
                a = getattr(obj, name)
                if isinstance(a, uuid.UUID):
                    a = a.hex
                attr[name] = a
            return attr

        def key_for(self, obj):
            name = self.pk.name
            attr = getattr(obj, name)
            if isinstance(attr, uuid.UUID):
                attr = attr.hex
            return attr

        def lookup(self, name):
            return self.cls.get(self.pk == name)

        def create(self, data):
            return self.cls.create(**data)

        def delete(self, name):
            self.cls.delete().where(self.pk == name).execute()

        def delete_list(self, selector):
            self.select_on(self.cls.delete(), selector).execute()

        def select_on(self, items, selector):
            for s in selector:
                key, operator, values = s.key, s.__class__, s.value
                field = self.fields[key]
                if operator == dom.Operator.Equals:
                    items = items.where(field == values)
                elif operator == dom.Operator.NotEquals:
                    items = items.where(field != values)
                else:
                    raise Exception('unsupported')
            return items

        def list(self, selector, limit, next):
            items = self.cls.select()
            pk = self.pk
            next_token = None
            if selector:
                items = self.select_on(items, selector)

            if limit or next:
                items = items.order_by(pk)
                if next:
                    items = items.where(pk > next)
                if limit:
                    items = items.limit(limit)

                items = list(items)
                if items:
                    next_token = self.key_for(items[-1])
            else:
                items = list(items)

            return Collection.List(
                name=self.name, 
                selector=dom.dump_selector(selector),
                items=items,
                next=next_token
            )



class Registry:
    def __init__(self, name=""):
        self.for_path = dict()

        self.for_type = dict()
        self.service = None
        if name:
            prefix="/{}/".format(name)
        else:
            prefix="/"
        self.prefix=prefix

    def register(self,obj, handler):
        obj.Handler = handler
        return self.add()(obj)

    def add(self, name=None):
        def _add(obj):
            if isinstance(obj, types.FunctionType):
                obj.Handler = FunctionHandler
            self.add_handler(name, obj.Handler, obj)
            return obj

        return _add

    def add_handler(self, name, handler, obj):
        n = obj.__name__ if name is None else name
        handler = handler(n, obj)

        if name in self.for_path or obj in self.for_type:
            raise Exception('dupe')
        self.for_path[n] = handler
        self.for_type[obj] = handler

        for cls in handler.subtypes():
            if cls in self.for_type:
                raise Exception('dupe')
            self.for_type[cls] = handler
        self.service = None

    def index(self):
        if self.service is None:
            actions = dict()
            links = []
            embeds = {}
            for name,o in self.for_path.items():
                inline = o.inline(self.prefix)
                if inline is not None:
                    links.append(name)
                    embeds[name] = inline
                else:
                    actions[name] = o.link(prefix=self.prefix)

            self.service = dom.Namespace('Index',
                metadata={'url':self.prefix,'links':links, 'embeds':embeds, 'actions':actions},
                attributes={},
            )
        return self.service

    def handle(self, request):
        path = request.path[:]
        if path == self.prefix or path == self.prefix[:-1]:
            out = self.index()
        elif path:
            p = len(self.prefix)
            path = path[p:]
            name = path.split('/',1)[0].split('.',1)[0]
            if name in self.for_path:
                data  = request.data.decode('utf-8')
                if data:
                    args = dom.parse(data)
                else:
                    args = None

                params = request.args

                context = {}
                request = dom.Request(
                    method=request.method,
                    url=path,
                    params=params, 
                    headers={},
                    data=args,
                )

                out = self.for_path[name].on_request(context, request)
            else:
                raise dom.NotFound(path)
        
        def transform(o):
            if isinstance(o, type) or isinstance(o, types.FunctionType):
                return self.for_type[o].embed(self.prefix, o)
            elif isinstance(o, types.MethodType):
                return self.for_type[o.__self__].embed(self.prefix, o)
            elif o.__class__ in self.for_type:
                return self.for_type[o.__class__].embed(self.prefix, o)
            elif isinstance(o, Embed):
                return o.embed(self.prefix, path)
            return o

        if out is None:
            return Response('', status='204 None')

        result = dom.dump(out, transform)
        return Response(result, content_type=dom.CONTENT_TYPE) 
    def app(self):
        return WSGIApp(self.handle)

class WSGIApp:
    def __init__(self, handler):
        self.handler = handler

    def __call__(self, environ, start_response):
        request = Request(environ)
        try:
            response = self.handler(request)
        except (StopIteration, GeneratorExit, SystemExit, KeyboardInterrupt):
            raise
        except HTTPException as r:
            response = r
            self.log_error(r, traceback.format_exc())
        except Exception as e:
            trace = traceback.format_exc()
            self.log_error(e, trace)
            response = self.error_response(e, trace)
        return response(environ, start_response)

    def log_error(self, exception, trace):
        print(trace, file=sys.stderr)

    def error_response(self, exception, trace):
        return Response(trace, status='500 not ok (%s)'%exception)

class QuietWSGIRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        pass

class Server(threading.Thread):
    def __init__(self, app, host="", port=0, request_handler=QuietWSGIRequestHandler):
        threading.Thread.__init__(self)
        self.daemon=True
        self.running = True
        self.server = make_server(host, port, app,
            handler_class=request_handler)

    @property
    def url(self):
        return u'http://%s:%d/'%(self.server.server_name, self.server.server_port)

    def run(self):
        self.running = True
        while self.running:
            self.server.handle_request()

    def stop(self):
        self.running =False
        if self.server and self.is_alive():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(self.server.socket.getsockname()[:2])
                s.send(b'\r\n')
                s.close()
            except IOError:
                import traceback
                traceback.print_exc()
        self.join(5)

from collections import OrderedDict
from urllib.parse import urljoin

reserved_tags = set("""
        bool int float complex
        string bytestring base64
        duration datetime
        set list dict object
        unknown
""".split())

class Registry:
    def __init__(self):
        self.classes = OrderedDict()
        self.tag_for = OrderedDict()

    def add(self, name=None):
        def _add(cls):
            n = cls.__name__ if name is None else name
            if n in reserved_tags:
                raise InvalidTag(
                    name, "Can't tag {} with {}, {} is reserved".format(cls, name, name))
            self.classes[n] = cls
            self.tag_for[cls] = n
            return cls
        return _add

    def as_tagged(self, obj):
        if obj.__class__ == TaggedObject:
            return obj.name, obj.value
        elif obj.__class__ in self.tag_for:
            name = self.tag_for[obj.__class__]
            return name, OrderedDict(obj.__dict__)
        else:
            raise InvalidTag('unknown',
                "Can't find tag for object {}: unknown class {}".format(obj, obj.__class__))

    def from_tag(self, name, value):
        if name in reserved_tags:
            raise InvalidTag(
                name, "Can't use tag {} with {}, {} is reserved".format(value, name, name))

        if name in self.classes:
            return self.classes[name](**value)
        else:
            return TaggedObject(name, value)


registry = Registry()

class InvalidTag(Exception):
    def __init__(self, name, reason):
        self.name = name
        Exception.__init__(self, reason)

class TaggedObject:
    def __init__(self, name, value):
        self.name, self.value = name,value

    def __repr__(self):
        return "<{} {}>".format(self.name, self.value)

class Hyperlink:
    pass

@registry.add()
class Link(Hyperlink):
    def __init__(self, url, value=None):
        self.url = url
        self.value = value

@registry.add()
class Form(Hyperlink):
    def __init__(self, url, arguments):
        self.url = url
        self.arguments = arguments

@registry.add()
class Selector(Hyperlink):
    def __init__(self, kind, url, arguments, selectors=()):
        self.kind = kind
        self.url = url
        self.arguments = arguments
        self.selectors = [] # {key,operator, value}

@registry.add()
class Resource(Hyperlink):
    def __init__(self, kind, metadata, attributes):
        self.kind = kind
        self.attributes = attributes
        self.metadata = OrderedDict(metadata)
    @property
    def url(self):
        return self.metadata['url']


@registry.add()
class Request:
    def __init__(self,method, url, headers, params, data):
        self.method = method
        self.url = url
        self.headers = headers
        self.params = params
        self.data = data

@registry.add()
class Response:
    def __init__(self, code, status, headers, data):
        self.code = code
        self.status = status
        self.headers = headers
        self.data = data


def tag_value_for_object(obj):
    return registry.as_tagged(obj)

def tag_rson_value(name, value):
    return registry.from_tag(name, value)




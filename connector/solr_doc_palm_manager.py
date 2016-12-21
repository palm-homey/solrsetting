# -*- coding: utf-8 -*-
# Copyright 2013-2014 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __builtin__ import str
from _ast import Str

"""Receives documents from the oplog worker threads and indexes them
into the backend.

This file is a document manager for the Solr search engine, but the intent
is that this file can be used as an example to add on different backends.
To extend this to other systems, simply implement the exact same class and
replace the method definitions with API calls for the desired backend.
"""
import json
import logging
import os
import re

from pysolr import Solr, SolrError

from mongo_connector import errors
from mongo_connector.compat import u
from mongo_connector.constants import (DEFAULT_COMMIT_INTERVAL,
                                       DEFAULT_MAX_BULK)
from mongo_connector.compat import (
    Request, urlopen, urlencode, URLError, HTTPError)
from mongo_connector.util import exception_wrapper, retry_until_ok
from mongo_connector.doc_managers.doc_manager_base import DocManagerBase
from mongo_connector.doc_managers.formatters import DocumentFlattener

wrap_exceptions = exception_wrapper({
    SolrError: errors.OperationFailed,
    URLError: errors.ConnectionFailed,
    HTTPError: errors.ConnectionFailed
})

ADMIN_URL = 'admin/luke?show=schema&wt=json'
# From the documentation of Solr 4.0 "classic" query parser.
ESCAPE_CHARACTERS = set('+-&|!(){}[]^"~*?:\\/')

decoder = json.JSONDecoder()


class DocManager(DocManagerBase):
    """The DocManager class creates a connection to the backend engine and
    adds/removes documents, and in the case of rollback, searches for them.

    The reason for storing id/doc pairs as opposed to doc's is so that multiple
    updates to the same doc reflect the most up to date version as opposed to
    multiple, slightly different versions of a doc.
    """

    def __init__(self, url, auto_commit_interval=DEFAULT_COMMIT_INTERVAL,
                 unique_key='_id', chunk_size=DEFAULT_MAX_BULK, **kwargs):
        """Verify Solr URL and establish a connection.
        """
        self.url = url
        self.solr = Solr(url, **kwargs.get('clientOptions', {}))
        self.unique_key = unique_key
        # pysolr does things in milliseconds
        if auto_commit_interval is not None:
            self.auto_commit_interval = auto_commit_interval * 1000
        else:
            self.auto_commit_interval = None
        self.chunk_size = chunk_size
        self.field_list = []
        self._build_fields()
        self._formatter = DocumentFlattener()
        
        self._content_type = kwargs.get("content_type",None)
        logging.info("begin to init content_type args ,value is %s" % str(self._content_type))
        
        if self._content_type is None:
            logging.info("content_type args is none, will receive all type")
            self._receive_all_type = True
        else:
            logging.debug("begin to check content_type args")
            self._receive_all_type = False
            if isinstance(self._content_type,dict):
                self._content_type_list = dict(self._content_type).keys()
                logging.debug("the support type list is %s" % str(self._content_type_list))
                    
            else:
                raise errors.InvalidConfiguration("args content type is not is dict")
                
            

    def _parse_fields(self, result, field_name):
        """ If Schema access, parse fields and build respective lists
        """
        field_list = []
        for key, value in result.get('schema', {}).get(field_name, {}).items():
            if key not in field_list:
                field_list.append(key)
        return field_list

    @wrap_exceptions
    def _build_fields(self):
        """ Builds a list of valid fields
        """
        declared_fields = self.solr._send_request('get', ADMIN_URL)
        result = decoder.decode(declared_fields)
        self.field_list = self._parse_fields(result, 'fields')

        # Build regular expressions to match dynamic fields.
        # dynamic field names may have exactly one wildcard, either at
        # the beginning or the end of the name
        self._dynamic_field_regexes = []
        for wc_pattern in self._parse_fields(result, 'dynamicFields'):
            if wc_pattern[0] == "*":
                self._dynamic_field_regexes.append(
                    re.compile(".*%s\Z" % wc_pattern[1:]))
            elif wc_pattern[-1] == "*":
                self._dynamic_field_regexes.append(
                    re.compile("\A%s.*" % wc_pattern[:-1]))

    def _clean_doc(self, doc, namespace, timestamp):
        """Reformats the given document before insertion into Solr.

        This method reformats the document in the following ways:
          - removes extraneous fields that aren't defined in schema.xml
          - unwinds arrays in order to find and later flatten sub-documents
          - flattens the document so that there are no sub-documents, and every
            value is associated with its dot-separated path of keys
          - inserts namespace and timestamp metadata into the document in order
            to handle rollbacks

        An example:
          {"a": 2,
           "b": {
             "c": {
               "d": 5
             }
           },
           "e": [6, 7, 8]
          }

        becomes:
          {"a": 2, "b.c.d": 5, "e.0": 6, "e.1": 7, "e.2": 8}

        """

        # Translate the _id field to whatever unique key we're using.
        # _id may not exist in the doc, if we retrieved it from Solr
        # as part of update.
        if '_id' in doc:
            doc[self.unique_key] = u(doc.pop("_id"))

        # Update namespace and timestamp metadata
        if 'ns' in doc or '_ts' in doc:
            raise errors.OperationFailed(
                'Need to set "ns" and "_ts" fields, but these fields already '
                'exist in the document %r!' % doc)
        doc['ns'] = namespace
        doc['_ts'] = timestamp
        
        # 获取mongo表名称
        collecion_name=self._get_collection_name(namespace)
        # 处理用户行为表数据
        # b_dynamic表不会再被处理的
        if( "b_dynamic" == collecion_name):
            logging.error("to process doc from b_dynamice ,the doc is %s" % str(doc[self.unique_key]))
            return None
            #return self._parse_user_dynamic_collection(doc)
        
        #处理用户表
        if("T_USER" == collecion_name):
            logging.info("to process doc from T_USER ,the doc is %s" % str(doc[self.unique_key]))
            return self._parse_t_user_collection(doc)      
        
        #to process the content data
        logging.info("begin to process b_content ,the doc is %s" % str(doc[self.unique_key]))
        doctemp=self._parse_content_doc(doc)
        
        if doctemp is None:
            logging.info("don't send doc to solr ,the doc is %s" % str(doc))
            return None
        
        if (isinstance(doctemp, list) and len(doctemp) == 0):
            logging.info("don't send doc to solr ,the doc is %s" % str(doc))
            return None
        
        if (isinstance(doctemp, list) and len(doctemp) >1 ):
            logging.info("to process doc from b_content after it is a list,the doc is %s" % str(doc[self.unique_key]))
            flat_doc=[]
            for docvalue in doctemp:
                flat_doc.append(self._parse_doc_to_solr_doc(docvalue))
                
            return flat_doc
        
        if (isinstance(doctemp, list)):
            logging.info("to process doc from b_content after it is a one-value list,the doc is %s" % str(doc[self.unique_key]))
            return self._parse_doc_to_solr_doc(doctemp[0])
        logging.info("to process doc from b_content after it is a object,the doc is %s" % str(doc[self.unique_key]))
        return self._parse_doc_to_solr_doc(doctemp)
        
    def _get_collection_name(self,namespace):
        '''获取mongodb的collection 的名称
        '''
        coll = namespace.split('.', 1)[1]
        return coll;            

    def _parse_user_dynamic_collection(self,doc):
        '''解析用户行为表，转换为搜索引擎识别的数据结构
        '''
        if doc.get("content"):
            doc["detail"]=doc.pop("content")
        if doc.get("createUser"):
            createUser=doc.get("createUser")
            author={}
            if createUser.get("userId"):
                author["id"]=createUser.get("userId")
            if createUser.get("userName"):
                author["name"]=createUser.get("userName")
            doc["author"]=[author]
        if doc.get("target"):
            doc["fkTag.0"]=doc.pop("target")
        
        
        return self._parse_doc_to_solr_doc(doc);
    
    def  _parse_t_user_collection(self,doc):
        '''解析用户表，转换为搜索引擎识别的数据结构
        '''
        #用户昵称转化
        nickName=doc.pop("nickName",None)
        if nickName:
            doc["title.0.name"]=nickName
            doc["tag.0.name"]=nickName
        #用户描述转化
        description=doc.pop("description",None)
        if description:
            doc["title.1.name"]=description
            doc["tag.1.name"]=description
        
        figureurl40=doc.pop("figureurl40",None)
        if figureurl40:
            doc["imgurl"]=figureurl40
            
        website=doc.pop("website",None)
        if website:
            doc["resurl"]=u"/u/"+str(website)
            doc["title.2.name"]=website
            doc["tag.2.name"]=website
        #如果用户被锁定说明用户不能被搜索
        isLocked=doc.pop("isLocked",None)
        if isLocked == "N":
            doc["status"]=u"released"
        else:
            doc["status"]=u"draft"
            
        #清除多余信息
        doc.pop("password",None)
        doc.pop("salt",None)
        doc.pop("phoneNum",None)
        doc.pop("userName",None)
        
        #补充必要信息
        doc["type"]=u"user"
        
        
        return self._parse_doc_to_solr_doc(doc);
   
            
    def _parse_doc_to_solr_doc(self,doc):
        # SOLR cannot index fields within sub-documents, so flatten documents
        # with the dot-separated path to each value as the respective key
        flat_doc = self._formatter.format_document(doc)

        # Only include fields that are explicitly provided in the
        # schema or match one of the dynamic field patterns, if
        # we were able to retrieve the schema
        if len(self.field_list) + len(self._dynamic_field_regexes) > 0:
            def include_field(field):
                return field in self.field_list or any(
                    regex.match(field) for regex in self._dynamic_field_regexes
                )
            return dict((k, v) for k, v in flat_doc.items() if include_field(k))
        return flat_doc

    
    def _parse_content_doc(self,doc):
        type=doc.get("type")
        if doc.get("releaseTime"):
            doc["createTime"]=doc.get("releaseTime")
        if (type == "product"):
            return self._parse_product(doc)
        #不再需要对图片视频文档等做特殊处理
        # if (type == "explain"):
        #     return self._parse_explain(doc)
        # elif(type == "video"):
        #     return self._parse_video(doc)
        # elif(type == "picture"):
        #     return self._paser_picture(doc)
        # else:
        return [doc]

    def _parse_product(self,doc):
        """
        处理项目数据，主要是对项目详情进行处理
        """
        spiltflag=False
        resultlist=[]
        #address-process
        address=doc.get("address")
        if address and isinstance(address,dict):
            #获取地址各个字段的数据
            adlist=[]
            self._add_list_with_not_empty_string(adlist,address.get("country"))
            self._add_list_with_not_empty_string(adlist,address.get("province"))
            self._add_list_with_not_empty_string(adlist,address.get("city"))
            self._add_list_with_not_empty_string(adlist,address.get("area"))
            self._add_list_with_not_empty_string(adlist,address.get("detail"))
            #合并为真的地址
            address_str="".join(adlist)
            if address_str:
                resultlist.append("项目地址:"+address_str)
        #开发建设方处理
        devbuilder=doc.get("devBuilder")
        if devbuilder and isinstance(devbuilder,list):
            devlist=[]
            for v in devbuilder:
                self._add_list_with_not_empty_string(devlist,v.get("name"))
            dev_str=",".join(devlist)
            if dev_str:
                resultlist.append("开发建设方:"+dev_str)
        #主要设计师处理
        maindesign=doc.get("buildingMainDesigner")
        if maindesign and isinstance(maindesign,list):
            designlist=[]
            for v in designlist:
                self._add_list_with_not_empty_string(designlist,v.get("name"))
            design_str=",".join(designlist)
            if design_str:
                resultlist.append("建筑主创设计师:"+design_str)
        #建筑面积处理
        buildingArea=doc.get("buildingArea")
        if buildingArea :
            resultlist.append("建筑面积:"+str(buildingArea)+"㎡")

        doc["detail"]=" / ".join(resultlist)
        return [doc]

    def _add_list_with_not_empty_string(self,v_list,value):
        if value :
            v_list.append(str(value))

    
    def _parse_explain(self,doc):
        """parse the content explain to replace the resurl value to be composited of fkTag
        """
        return [doc]
        ''' 不需要对explain即点评做特殊处理了
        fkTag=doc.get("fkTag")
        if(isinstance(fkTag,list) and len(fkTag) > 0):
            resurl="/detail/"+str(fkTag[0])
            
            logging.info("resurl is replace from %s to %s" % (doc.get("resurl"),resurl))
            doc["resurl"]=u(resurl)
        else:
            logging.error("fail to change resurl(%s) ,because the fkTag(%s) is not valid" % (str(doc.get("resurl")),str(doc.get("fkTag")) ))
        return [doc] 
        '''   
    
    def _parse_video(self,doc):
        
        return self._parse_content_list_to_serval(doc,"video","video")
        
    def _paser_picture(self,doc):
        """parse the picture content to subdoc 
        
        doclist=[doc]
        logging.debug("parse picture ,the raw doc is %s:" % str(doc))
        picture=doc.get("picture")
        
        if(isinstance(picture, list) and len(picture)>0):
            
            for index,value in enumerate(picture):
                doctemp=doc.copy()
                doctemp["s_picture_id"]=u(value.get("id"))
                doctemp["s_pitcure_name"]=u(value.get("name"))
                doctemp["_id"]=u(doctemp.get("_id")+"_"+str(index))
                doctemp["s_parent_id"]=u(doctemp.get("_id"))
                doctemp["type"]="s_picture"
                doclist.append(doctemp)
            #only picture is existed , to replace s_picture attr
            doc["s_picture"]=picture
        
        # !!!!!there is bug when update picture status  
        return doclist
        """
        return self._parse_content_list_to_serval(doc, "picture", "picture")
    
    def _parse_content_list_to_serval(self,doc,fieldName,type):
        """parse the picture content to subdoc 
        """
        doclist=[doc]
        logging.debug("parse %s ,the raw doc is %s:" % (fieldName,str(doc)))
        picture=doc.get(fieldName)
        
        if(isinstance(picture, list) and len(picture)>0):
            s_field_id="s_"+fieldName+"_id"
            s_field_name="s_"+fieldName+"_name"
            new_type="s_"+type
            for index,value in enumerate(picture):
                doctemp=doc.copy()
                doctemp[s_field_id]=u(value.get("id"))
                doctemp[s_field_name]=u(value.get("name"))
                doctemp["_id"]=u(doctemp.get("_id")+"_"+str(index))
                doctemp["s_parent_id"]=u(doctemp.get("_id"))
                doctemp["type"]=new_type
                doclist.append(doctemp)
            #only picture is existed , to replace s_picture attr
            doc["s_"+fieldName]=picture
        
        # !!!!!there is bug when update picture status  
        return doclist

    def stop(self):
        """ Stops the instance
        """
        pass

    @wrap_exceptions
    def handle_command(self, doc, namespace, timestamp):
        db, _ = namespace.split('.', 1)
        if doc.get('dropDatabase'):
            for new_db in self.command_helper.map_db(db):
                self.solr.delete(q="ns:%s.*" % new_db,
                                 commit=(self.auto_commit_interval == 0))

        if doc.get('renameCollection'):
            raise errors.OperationFailed(
                "solr_doc_manager does not support replication of "
                " renameCollection")

        if doc.get('create'):
            # nothing to do
            pass

        if doc.get('drop'):
            new_db, coll = self.command_helper.map_collection(db, doc['drop'])
            if new_db:
                self.solr.delete(q="ns:%s.%s" % (new_db, coll),
                                 commit=(self.auto_commit_interval == 0))

    def apply_update(self, doc, update_spec):
        """Override DocManagerBase.apply_update to have flat documents."""
        # Replace a whole document
        if not '$set' in update_spec and not '$unset' in update_spec:
            # update_spec contains the new document.
            # Update the key in Solr based on the unique_key mentioned as
            # parameter.
            update_spec['_id'] = doc[self.unique_key]
            return update_spec
        for to_set in update_spec.get("$set", []):
            value = update_spec['$set'][to_set]
            # Find dotted-path to the value, remove that key from doc, then
            # put value at key:
            keys_to_pop = []
            for key in doc:
                if key.startswith(to_set):
                    if key == to_set or key[len(to_set)] == '.':
                        keys_to_pop.append(key)
                #remove dotted-path the value ,with start with "s_"+to_set
                tmp_to_set="s_"+to_set
                if key.startswith(tmp_to_set):
                    if key == tmp_to_set or key[len(tmp_to_set)] == '.':
                        keys_to_pop.append(key)
            for key in keys_to_pop:
                doc.pop(key)
            doc[to_set] = value
        for to_unset in update_spec.get("$unset", []):
            # MongoDB < 2.5.2 reports $unset for fields that don't exist within
            # the document being updated.
            keys_to_pop = []
            for key in doc:
                if key.startswith(to_unset):
                    if key == to_unset or key[len(to_unset)] == '.':
                        keys_to_pop.append(key)
                tmp_to_unset="s_"+to_unset
                if key.startswith(tmp_to_unset):
                    if key == tmp_to_unset or key[len(tmp_to_unset)] == '.':
                        keys_to_pop.append(key)
            for key in keys_to_pop:
                doc.pop(key)
        return doc

    @wrap_exceptions
    def update(self, document_id, update_spec, namespace, timestamp):
        """Apply updates given in update_spec to the document whose id
        matches that of doc.

        """
        # Commit outstanding changes so that the document to be updated is the
        # same version to which the changes apply.
        self.commit()
        # Need to escape special characters in the document_id.
        document_id = ''.join(map(
            lambda c: '\\' + c if c in ESCAPE_CHARACTERS else c,
            u(document_id)
        ))

        query = "%s:%s" % (self.unique_key, document_id)
        results = self.solr.search(query)
        if not len(results):
            # Document may not be retrievable yet
            self.commit()
            results = self.solr.search(query)
        # Results is an iterable containing only 1 result
        for doc in results:
            # Remove metadata previously stored by Mongo Connector.
            doc.pop('ns')
            doc.pop('_ts')
            updated = self.apply_update(doc, update_spec)
            # A _version_ of 0 will always apply the update
            updated['_version_'] = 0
            self.upsert(updated, namespace, timestamp)
            return updated

    @wrap_exceptions
    def upsert(self, doc, namespace, timestamp):
        """Update or insert a document into Solr

        This method should call whatever add/insert/update method exists for
        the backend engine and add the document in there. The input will
        always be one mongo document, represented as a Python dictionary.
        """
        logging.debug("before insert the raw doc is :(%s)" % str(doc))
        docs=self._clean_doc(doc,namespace,timestamp)
        logging.debug("before insert the processed doc is :(%s)" % str(doc))
        if docs is None:
            return None
        if not isinstance(docs,list):
            docs= [docs]
        docid=doc.get("_id")
        #self.remove(docid, namespace, timestamp)
        #delete the child node about this file, TODO
        # if docid :
        #     logging.info("remove solr document which id is %s _* ,timestamp is %s" % (str(docid), str(timestamp)))
        #     self.solr.delete(q=u("_id:"+docid+"_*"),
        #                      commit=(self.auto_commit_interval == 0))
        # else:
        #     raise errors.OperationFailed("delete solr document error for the id(%s) is not valid" % str(docid));
        try:
            if self.auto_commit_interval is not None:
                self.solr.add(docs,
                              commit=(self.auto_commit_interval == 0),
                              commitWithin=u(self.auto_commit_interval))
            else:
                self.solr.add(docs,
                              commit=False)
            logging.debug("insert into solr docs:(%s)" % str(docs))
        except UnicodeDecodeError:
            logging.exception(
                "Unable to process processed document for UnicodeDecodeError, %r " % str(docs))

    @wrap_exceptions
    def bulk_upsert(self, docs, namespace, timestamp):
        """Update or insert multiple documents into Solr

        docs may be any iterable
        """
        if self.auto_commit_interval is not None:
            add_kwargs = {
                "commit": (self.auto_commit_interval == 0),
                "commitWithin": str(self.auto_commit_interval)
            }
        else:
            add_kwargs = {"commit": False}

        cleaned = (self._clean_doc(d, namespace, timestamp) for d in docs)
        if self.chunk_size > 0:
            batch = list(next(cleaned) for i in range(self.chunk_size))
            while batch:
                self.solr.add(batch, **add_kwargs)
                batch = list(next(cleaned)
                             for i in range(self.chunk_size))
        else:
            self.solr.add(cleaned, **add_kwargs)

    @wrap_exceptions
    def insert_file(self, f, namespace, timestamp):
        params = self._formatter.format_document(f.get_metadata())
        params[self.unique_key] = params.pop('_id')
        params['ns'] = namespace
        params['_ts'] = timestamp
        params = dict(('literal.' + k, v) for k, v in params.items())

        if self.auto_commit_interval == 0:
            params['commit'] = 'true'

        request = Request(os.path.join(
            self.url, "update/extract?%s" % urlencode(params)))

        request.add_header("Content-type", "application/octet-stream")
        request.data = f
        response = urlopen(request)
        logging.debug(response.read())

    @wrap_exceptions
    def remove(self, document_id, namespace, timestamp):
        """Removes documents from Solr

        The input is a python dictionary that represents a mongo document.
        """
        if document_id :
            self.solr.delete(id=u(document_id),
                             commit=(self.auto_commit_interval == 0))
            self.solr.delete(q=u("_id:"+document_id+"_*"),
                             commit=(self.auto_commit_interval == 0))
        else:
            raise errors.OperationFailed("delete solr document error for the id(%s) is not valid" % str(document_id));

    @wrap_exceptions
    def _stream_search(self, query):
        """Helper method for iterating over Solr search results."""
        for doc in self.solr.search(query, rows=100000000):
            if self.unique_key != "_id":
                doc["_id"] = doc.pop(self.unique_key)
            yield doc

    @wrap_exceptions
    def search(self, start_ts, end_ts):
        """Called to query Solr for documents in a time range."""
        query = '_ts: [%s TO %s]' % (start_ts, end_ts)
        return self._stream_search(query)

    def commit(self):
        """This function is used to force a commit.
        """
        retry_until_ok(self.solr.commit)

    @wrap_exceptions
    def get_last_doc(self):
        """Returns the last document stored in the Solr engine.
        """
        #search everything, sort by descending timestamp, return 1 row
        try:
            result = self.solr.search('*:*', sort='_ts desc', rows=1)
        except ValueError:
            return None

        for r in result:
            r['_id'] = r.pop(self.unique_key)
            return r

if __name__ == "__main__":
    docm=DocManager("")
    print(docm._parse_product({}))
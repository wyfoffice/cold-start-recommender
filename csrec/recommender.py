from collections import defaultdict
import pandas as pd
import numpy as np
from time import time
import logging
import json
from tools.Singleton import Singleton


class Recommender(Singleton):
    """
    Cold Start Recommender
    """
    def __init__(self, db, max_rating=5, log_level=logging.DEBUG):
        # Logger initialization
        self.logger = logging.getLogger("csrc")
        self.logger.setLevel(log_level)
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        self.logger.addHandler(ch)
        self.logger.debug("============ Logger initialized ================")

        # initialization of datastore attribute
        self.db = db

        # registering callback functions for datastore events
        self.db.register(self.db.insert_item, self.on_insert_item)
        # self.db.register(self.db.remove_item, self.on_remove_item)
        # self.db.register(self.db.insert_or_update_item_action, self.on_insert_or_update_item_action)
        # self.db.register(self.db.remove_item_action, self.on_remove_item_rating)
        # self.db.register(self.db.reconcile_user, self.on_reconcile_user)
        # self.db.register(self.db.serialize, self.on_serialize)
        # self.db.register(self.db.restore, self.on_restore)

        # Algorithm's specific attributes
        self._items_cooccurrence = pd.DataFrame  # cooccurrence of items
        self.cooccurrence_updated = 0.0
        # Info in item_meaningful_info with whom some user has actually interacted
        self.info_used = set()
        self._categories_cooccurrence = {}  # cooccurrence of categories

        # categories --same as above, but separated as they are not always available
        #TODO to be in the DAL
        self.tot_categories_user_ratings = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # sum of all ratings  (inmemory testing)
        self.tot_categories_item_ratings = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # ditto
        self.n_categories_user_ratings = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # number of ratings  (inmemory testing)
        #END TODO
        self.items_by_popularity = []  # can be recomputed on_restore
        self.last_serialization_time = 0.0  # Time of data backup
        # configurations:
        self.max_rating = max_rating

    def on_insert_item(self, item_id, attributes, return_value=None):
        if not return_value:
            return
        self.db.insert_item(item_id=item_id, attributes=attributes)

    def remove_item(self, item_id):
        self.db.remove_item(item_id=item_id)

    def insert_item_action(self, user_id, item_id, code=3.0, item_meaningful_info=None, only_info=False):
        """

        self.item_meaningful_info can be any further information given with the dict item.
        e.g. author, category etc

        NB NO DOTS IN user_id, or they will be taken away. Fields in mongodb cannot have dots..

        If self.only_info==True, only the self.item_meaningful_info's are put in the co-occurrence, not item_id.
         This is necessary when we have for instance a "segmentation page" where we propose
         well known items to get to know the user. If s/he select "Harry Potter" we only want
         to retrieve the info that s/he likes JK Rowling, narrative, magic etc

        :param user_id: id of user. NO DOTS, or they will taken away. Fields in mongodb cannot have dots.
        :param item_id: is either id or a dict with item_id_key
        :param code: float parseable
        :return: None
        """
        if not item_meaningful_info:
            item_meaningful_info = []

        # If self.only_info==True, only the self.item_meaningful_info's are put in the co-occurrence, not item_id.
        # This is necessary when we have for instance a "segmentation page" where we propose
        # well known items to get to know the user. If s/he select "Harry Potter" we only want
        # to retrieve the info that s/he likes JK Rowling, narrative, magic etc

        # Now fill the dicts or the db collections if available
        user_id = str(user_id).replace('.', '')

        item = self.db.get_item(item_id)
        if item:
            # Do categories only if the item is stored
            if len(item_meaningful_info) > 0:
                for k, v in item.items():
                    if k in item_meaningful_info:
                        # Some items' attributes are lists (e.g. tags: [])
                        # or, worse, string which can represent lists...
                        try:
                            v = json.loads(v.replace("'", '"'))
                        except:
                            pass

                        if not hasattr(v, '__iter__'):
                            values = [v]
                        else:
                            values = v
                        self.info_used.add(k)
                        # we cannot set the rating, because we want to keep the info
                        # that a user has read N books of, say, the same author,
                        # category etc.
                        # We could, but won't, sum all the ratings and count the a result as "big rating".
                        # We won't because reading N books of author A and rating them 5 would be the same
                        # as reading 5*N books of author B and rating them 1.
                        # Therefore we take the average because --
                        # 1) we don't want ratings for category to skyrocket
                        # 2) if a user changes their idea on rating a book, it should not add up.
                        # Average is not perfect, but close enough.
                        #
                        # Take total number of ratings and total rating:
                        for value in values:
                            if len(str(value)) > 0:
                                self.tot_categories_user_ratings[k][user_id][value] += int(code)
                                self.n_categories_user_ratings[k][user_id][value] += 1
                                # for the co-occurrence matrix is not necessary to do the same for item, but better do it
                                # in case we want to compute similarities etc using categories
                                self.tot_categories_item_ratings[k][value][user_id] += int(code)
        else:
            self.db.insert_item(item_id=item_id)
        if not only_info:
            self.db.insert_item_action(user_id=user_id, item_id=item_id, code=code)

    def remove_item_rating(self, user_id, item_id):
        self.db.remove_item(user_id=user_id, item_id=item_id)

    def reconcile_user(self, old_user_id, new_user_id):
        self.db.reconcile_user(old_user_id=old_user_id, new_user_id=new_user_id)

    def on_serialize(self, filepath, return_value):
        if return_value:
            self.last_serialization_time = time()
        else:
            self.logger.error("[on_serialize] data backup failed on file %s, last successful backup at: %f" %
                              (filepath,
                               self.last_serialization_time))

    def on_restore(self, filepath, return_value):
        if not return_value:
            self.logger.error("[on_restore] restore from serialized data fail: ", filepath)

        self._create_cooccurrence()
        r_it = self.db.get_item_ratings_iterator()
        for item in r_it:
            user_id = item[0]
            ratings = item[1]
            for item_id, rating in ratings.items():
                self.insert_item_action(user_id=user_id, item_id=item_id, code=rating)

    def _create_cooccurrence(self):
        """
        Create or update the co-occurrence matrix
        :return:
        """
        all_ratings = self.db.get_all_users_item_actions()
        df = pd.DataFrame(all_ratings).fillna(0).astype(int)  # convert dictionary to pandas dataframe

        #calculate co-occurrence matrix
        # sometime will print the warning: "RuntimeWarning: invalid value encountered in true_divide"
        # use np.seterr(divide='ignore', invalid='ignore') to suppress this warning
        df_items = (df / df).replace(np.inf, 0).replace(np.nan,0) #calculate co-occurrence matrix and normalize to 1
        co_occurrence = df_items.fillna(0).dot(df_items.T)
        self._items_cooccurrence = co_occurrence

        #update co-occurrence matrix for items categories
        df_tot_cat_item = {}

        if len(self.info_used) > 0:

            for i in self.info_used:
                df_tot_cat_item[i] = pd.DataFrame(self.tot_categories_item_ratings[i]).fillna(0).astype(int)

            for i in self.info_used:
                if type(df_tot_cat_item.get(i)) == pd.DataFrame:
                    df_tot_cat_item[i] = (df_tot_cat_item[i] / df_tot_cat_item[i]).replace(np.inf, 0)
                    self._categories_cooccurrence[i] = df_tot_cat_item[i].T.dot(df_tot_cat_item[i])

        self.cooccurrence_updated = time()

    def compute_items_by_popularity(self):
        """
        As per name, get self.
        :return: list of popular items, 0=most popular
        """
        df_item = pd.DataFrame(self.db.get_all_users_item_actions()).T.fillna(0).astype(int).sum()
        df_item.sort(ascending=False)
        pop_items = list(df_item.index)
        all_items = set(self.db.get_all_items().keys())
        self.items_by_popularity = (pop_items + list(all_items - set(pop_items)))

    def get_recommendations(self, user_id, max_recs=50, fast=False, algorithm='item_based'):
        """
        algorithm item_based:
            - Compute recommendation to user using item co-occurrence matrix (if the user
            rated any item...)
            - If there are less than max_recs recommendations, the remaining
            items are given according to popularity. Scores for the popular ones
            are given as score[last recommended]*index[last recommended]/n
            where n is the position in the list.
            - Recommended items above receive a further score according to categories
        :param user_id: the user id as in the mongo collection 'users'
        :param max_recs: number of recommended items to be returned
        :param fast: Compute the co-occurrence matrix only if it is one hour old or
                     if matrix and user vector have different dimension
        :return: list of recommended items
        """
        user_id = str(user_id).replace('.', '')
        df_tot_cat_user = {}
        df_n_cat_user = {}
        rec = pd.Series()
        user_has_rated_items = False  # has user rated some items?
        rated_infos = []  # user has rated the category (e.g. the category "author" etc)
        df_user = None
        if self.db.get_user_item_actions(user_id):  # compute item-based rec only if user has rated smt
            user_has_rated_items = True
            # Just take user_id for the user vector
            df_user = pd.DataFrame(self.db.get_all_users_item_actions()).fillna(0).astype(int)[[user_id]]
        if len(self.info_used) > 0:
            for i in self.info_used:
                if self.tot_categories_user_ratings[i].get(user_id):
                    rated_infos.append(i)
                    df_tot_cat_user[i] = pd.DataFrame(self.tot_categories_user_ratings[i]).fillna(0).astype(int)[[user_id]]
                    df_n_cat_user[i] = pd.DataFrame(self.n_categories_user_ratings[i]).fillna(0).astype(int)[[user_id]]

        if user_has_rated_items:
            if not fast or (time() - self.cooccurrence_updated > 1800):
                self._create_cooccurrence()
            try:
                # this might fail if the cooccurrence was not updated (fast)
                # and the user rated a new item.
                # In this case the matrix and the user-vector have different
                # dimension
                # print("DEBUG [get_recommendations] Trying cooccurrence dot df_user")
                # print("DEBUG [get_recommendations] _items_cooccurrence: %s", self._items_cooccurrence)
                # print("DEBUG [get_recommendations] df_user: %s", df_user)
                rec = self._items_cooccurrence.T.dot(df_user[user_id])
                # self.logger.debug("[get_recommendations] Rec worked: %s", rec)
            except:
                self.logger.debug("[get_recommendations] 1st rec production failed, calling _create_cooccurrence.")
                self._create_cooccurrence()
                rec = self._items_cooccurrence.T.dot(df_user[user_id])
                self.logger.debug("[get_recommendations] Rec: %s", rec)
            # Sort by cooccurrence * rating:
            rec.sort(ascending=False)

            # If necessary, add popular items
            if len(rec) < max_recs:
                if not fast or len(self.items_by_popularity) == 0:
                    self.compute_items_by_popularity()
                for v in self.items_by_popularity:
                    if len(rec) == max_recs:
                        break
                    elif v not in rec.index:
                        n = len(rec)
                        # supposing score goes down according to Zipf distribution
                        rec.set_value(v, rec.values[n - 1]*n/(n+1.))

        else:
            if not fast or len(self.items_by_popularity) == 0:
                self.compute_items_by_popularity()
            for i, v in enumerate(self.items_by_popularity):
                if len(rec) == max_recs:
                    break
                rec.set_value(v, self.max_rating / (i+1.))  # As comment above, starting from max_rating
#        print("DEBUG [get_recommendations] Rec after item_based or not: %s", rec)

        # Now, the worse case we have is the user has not rated, then rec=popular with score starting from max_rating
        # and going down as 1/i

        # User info on rated categories (in info_used)
        global_rec = rec.copy()
        if len(self.info_used) > 0:
            cat_rec = {}
            if not fast or (time() - self.cooccurrence_updated > 1800):
                self._create_cooccurrence()
            for cat in rated_infos:
                # get average rating on categories
                user_vec = df_tot_cat_user[cat][user_id] / df_n_cat_user[cat][user_id].replace(0, 1)
                # print("DEBUG [get_recommendations]. user_vec:\n", user_vec)
                try:
                    cat_rec[cat] = self._categories_cooccurrence[cat].T.dot(user_vec)
                    cat_rec[cat].sort(ascending=False)
                    #print("DEBUG [get_recommendations] cat_rec (try):\n %s", cat_rec)
                except:
                    self._create_cooccurrence()
                    cat_rec[cat] = self._categories_cooccurrence[cat].T.dot(user_vec)
                    cat_rec[cat].sort(ascending=False)
                    #print("DEBUG [get_recommendations] cat_rec (except):\n %s", cat_rec)
                for item_id, score in rec.iteritems():
                    #print("DEBUG [get_recommendations] rec_item_id: %s", k)
                    try:
                        item_info_value = self.db.get_item_value(item_id=item_id, key=cat)

                        #print("DEBUG get_recommendations. item value for %s: %s", cat, item_info_value)
                        # In case the info value is not in cat_rec (as it can obviously happen
                        # because a rec'd item coming from most popular can have the value of
                        # an info (author etc) which is not in the rec'd info
                        if item_info_value:
                            global_rec[item_id] = score + cat_rec.get(cat, {}).get(item_info_value, 0)
                    except Exception, e:
                        self.logger.error("item %s, category %s", item_id, cat)
                        logging.exception(e)
        global_rec.sort(ascending=False)
#        print("DEBUG [get_recommendations] global_rec:\n %s", global_rec)

        if user_has_rated_items:
            # If the user has rated all items, return an empty list
            rated = df_user[user_id] != 0
            # rated.get is correct (pycharm complains, knows no pandas)
            return [i for i in global_rec.index if not rated.get(i, False)][:max_recs]
        else:
            try:
                return list(global_rec.index)[:max_recs]
            except:
                return None

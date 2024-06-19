from flask import Flask, request, jsonify
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from surprise import Dataset, Reader, SVD
import re
import nltk
from Sastrawi.StopWordRemover.StopWordRemoverFactory import StopWordRemoverFactory

# Download NLTK stop words
nltk.download('stopwords')

app = Flask(__name__)

# Function to clean text data
def clean_text(text):
    text = re.sub(r'[^\w\s]', '', text)
    text = text.lower()
    return text

# Function to get score by index from a list of scores
def get_score_by_idx(scores_list, idx):
    for score in scores_list:
        if score[0] == idx:
            return score[1]
    return None

# Function to load and clean data
def load_and_clean_data(products_file, reviews_file):
    df_products = pd.read_csv(products_file)
    df_reviews = pd.read_csv(reviews_file)

    # Clean product data
    df_products_cleaned = df_products.copy()
    df_products_cleaned['deskripsi'] = df_products_cleaned['deskripsi'].apply(clean_text)
    df_products_cleaned['kategori'] = df_products_cleaned['kategori'].apply(clean_text)
    df_products_cleaned.drop_duplicates(subset=['id_produk'], keep='first', inplace=True)
    df_products_cleaned['combined_features'] = df_products_cleaned['deskripsi'].fillna('') + ' ' + df_products_cleaned['kategori'].fillna('')

    return df_products, df_reviews, df_products_cleaned

# Function to create TF-IDF matrix and calculate cosine similarity
def calculate_tfidf_cosine_similarity(df):
    factory = StopWordRemoverFactory()
    stop_words_indonesia = factory.get_stop_words()
    stop_words_english = nltk.corpus.stopwords.words('english')
    combined_stop_words = stop_words_indonesia + stop_words_english

    tfidf = TfidfVectorizer(stop_words=combined_stop_words)
    tfidf_matrix = tfidf.fit_transform(df['combined_features'])
    cosine_sim_tfidf = cosine_similarity(tfidf_matrix, tfidf_matrix)

    return cosine_sim_tfidf

# Function to prepare data for collaborative filtering
def prepare_collaborative_filtering_data(df_reviews):
    reader = Reader(rating_scale=(1, 5))
    data = Dataset.load_from_df(df_reviews[['id_user', 'id_produk', 'rating_user']], reader)
    trainset = data.build_full_trainset()
    algo = SVD()
    algo.fit(trainset)

    return algo

# Function to create pivot table for collaborative filtering
def create_collaborative_filtering_pivot(df_reviews, df_products):
    users = df_reviews['id_user'].unique()
    products = df_products['id_produk'].unique()
    all_combinations = pd.DataFrame([(user, prod) for user in users for prod in products], columns=['id_user', 'id_produk'])
    merged_data = pd.merge(all_combinations, df_reviews, on=['id_user', 'id_produk'], how='left')
    merged_data.fillna(0, inplace=True)
    pivot_table = merged_data.pivot_table(index='id_user', columns='id_produk', values='rating_user', fill_value=0)
    cosine_sim_cf = cosine_similarity(pivot_table)

    return pivot_table, cosine_sim_cf

# Function to get recommendations
def get_recommendations(product_id, user_id, df_products, indices, cosine_sim_tfidf, cosine_sim_cf, algo, n_recommendations=10):
    try:
        idx = indices[product_id]
    except KeyError:
        return f"Product ID '{product_id}' not found.", 404
    
    if user_id not in df_reviews['id_user'].unique():
        return f"User ID '{user_id}' not found.", 404

    sim_scores_tfidf = list(enumerate(cosine_sim_tfidf[idx]))
    sim_scores_cf = list(enumerate(cosine_sim_cf[idx]))

    sim_scores_tfidf = sorted(sim_scores_tfidf, key=lambda x: x[1], reverse=True)
    sim_scores_cf = sorted(sim_scores_cf, key=lambda x: x[1], reverse=True)

    sim_scores_tfidf = sim_scores_tfidf[1:n_recommendations+1]
    sim_scores_cf = sim_scores_cf[1:n_recommendations+1]

    combined_scores = {}
    for score in sim_scores_tfidf:
        combined_scores[score[0]] = combined_scores.get(score[0], 0) + score[1]
    for score in sim_scores_cf:
        combined_scores[score[0]] = combined_scores.get(score[0], 0) + score[1]
    
    combined_scores = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
    top_indices = [i[0] for i in combined_scores[:n_recommendations]]

    mf_scores = []
    for i in top_indices:
        if i < len(df_products):
            mf_scores.append((i, algo.predict(user_id, df_products.iloc[i]['id_produk']).est))
    
    mf_scores = sorted(mf_scores, key=lambda x: x[1], reverse=True)
    final_indices = [i[0] for i in mf_scores[:n_recommendations]]

    recommendations = []
    for idx in final_indices:
        product_info = df_products.iloc[idx].to_dict()
        product_info['tfidf_score'] = get_score_by_idx(combined_scores, idx)
        product_info['cf_score'] = get_score_by_idx(mf_scores, idx)
        
        recommendations.append(product_info)

    return recommendations

# Function to get user-based recommendations
def get_user_based_recommendations(user_id, df_products, pivot_table, algo, n_recommendations=50):
    if user_id not in pivot_table.index:
        return f"User ID '{user_id}' not found.", 404

    user_idx = pivot_table.index.get_loc(user_id)
    user_ratings = pivot_table.loc[user_id]

    # Find similar users based on cosine similarity
    similar_users = cosine_similarity(pivot_table)
    similar_users_scores = list(enumerate(similar_users[user_idx]))
    similar_users_scores = sorted(similar_users_scores, key=lambda x: x[1], reverse=True)
    similar_users_scores = similar_users_scores[1:n_recommendations+1]

    recommended_products = {}
    for user in similar_users_scores:
        similar_user_idx = user[0]
        similar_user_ratings = pivot_table.iloc[similar_user_idx]
        for product_id, rating in similar_user_ratings.items():
            if rating > 0 and product_id not in user_ratings[user_ratings > 0].index:
                if product_id in recommended_products:
                    recommended_products[product_id].append(rating)
                else:
                    recommended_products[product_id] = [rating]

    # Average the ratings and ensure they are within the 0-5 range
    for product_id in recommended_products:
        recommended_products[product_id] = min(5, sum(recommended_products[product_id]) / len(recommended_products[product_id]))

    # Predict ratings using matrix factorization
    mf_scores = []
    for product_id in df_products['id_produk'].unique():
        if product_id not in recommended_products:
            pred_rating = algo.predict(user_id, product_id).est
            mf_scores.append((product_id, pred_rating))
    
    mf_scores = sorted(mf_scores, key=lambda x: x[1], reverse=True)

    # Combine CF and MF scores
    final_recommendations = {}
    for product_id, score in recommended_products.items():
        final_recommendations[product_id] = final_recommendations.get(product_id, 0) + score
    
    for product_id, score in mf_scores:
        final_recommendations[product_id] = final_recommendations.get(product_id, 0) + score
    
    final_recommendations = sorted(final_recommendations.items(), key=lambda x: x[1], reverse=True)

    recommendations = []
    for product_id, score in final_recommendations[:n_recommendations]:
        idx = indices[product_id]
        product_info = df_products.iloc[idx].to_dict()
        product_info['cf_score'] = score  # score di sini adalah skor kombinasi CF dan MF

        recommendations.append(product_info)

    return recommendations

def get_unrated_products(user_id, df_reviews, max_products=50):
    # Filter data untuk produk yang belum pernah diberi rating oleh user_id
    user_reviews = df_reviews[df_reviews['id_user'] == user_id]
    rated_products = set(user_reviews['id_produk'])
    all_products = set(df_products['id_produk'])
    unrated_products = list(all_products - rated_products)[:max_products]
    
    return unrated_products

@app.route('/unrated-products', methods=['GET'])
def unrated_products():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Please provide user_id"}), 400
    
    unrated_product_ids = get_unrated_products(user_id, df_reviews)
    
    unrated_products_info = []
    for product_id in unrated_product_ids:
        idx = indices.get(product_id)
        if idx is not None:
            product_info = df_products.iloc[idx].to_dict()
            unrated_products_info.append(product_info)
    
    return jsonify(unrated_products_info)

# Route to recommend endpoint
@app.route('/recommend', methods=['GET'])
def recommend():
    product_id = request.args.get('product_id')
    user_id = request.args.get('user_id')
    if not product_id or not user_id:
        return jsonify({"error": "Please provide both product_id and user_id"}), 400

    recommendations = get_recommendations(product_id, user_id, df_products, indices, cosine_sim_tfidf, cosine_sim_cf, algo)
    if isinstance(recommendations, tuple):
        return jsonify({"error": recommendations[0]}), recommendations[1]

    return jsonify(recommendations)

# Route to recommend user-based endpoint
@app.route('/recommend_user_based', methods=['GET'])
def recommend_user_based():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Please provide user_id"}), 400

    recommendations = get_user_based_recommendations(user_id, df_products, pivot_table, algo)
    if isinstance(recommendations, tuple):
        return jsonify({"error": recommendations[0]}), recommendations[1]

    return jsonify(recommendations)

# Route to get all products
@app.route('/products', methods=['GET'])
def get_all_products():
    products = df_products.to_dict(orient='records')
    return jsonify(products)

# Route to get product by ID
@app.route('/products/<product_id>', methods=['GET'])
def get_product_by_id(product_id):
    product = df_products[df_products['id_produk'] == product_id]
    if product.empty:
        return jsonify({"error": f"Product ID '{product_id}' not found"}), 404

    product_info = product.iloc[0].to_dict()
    return jsonify(product_info)

# Route to get all users
@app.route('/users', methods=['GET'])
def get_all_users():
    users = df_reviews[['id_user']].drop_duplicates().to_dict(orient='records')
    return jsonify(users)

# Route to get user by ID
@app.route('/users/<user_id>', methods=['GET'])
def get_user_by_id(user_id):
    user = df_reviews[df_reviews['id_user'] == user_id]
    if user.empty:
        return jsonify({"error": f"User ID '{user_id}' not found"}), 404

    user_info = user.to_dict(orient='records')
    return jsonify(user_info)

if __name__ == '__main__':
    # Load and clean data
    df_products, df_reviews, df_products_cleaned = load_and_clean_data('../data-collection-preprocessing/data-produk/clean_product-goodgamingshop.csv', '../data-collection-preprocessing/data-ulasan-clean/clean_data-ulasan-goodgamingstore.csv')

    # Calculate TF-IDF cosine similarity
    cosine_sim_tfidf = calculate_tfidf_cosine_similarity(df_products_cleaned)

    # Prepare data for collaborative filtering
    algo = prepare_collaborative_filtering_data(df_reviews)

    # Create pivot table for collaborative filtering
    pivot_table, cosine_sim_cf = create_collaborative_filtering_pivot(df_reviews, df_products)

    # Create mapping indices and ID produk
    indices = pd.Series(df_products.index, index=df_products['id_produk']).drop_duplicates()

    # Run Flask app
    app.run(debug=True)

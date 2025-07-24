module Data.Products exposing (Product, Review, allProducts, getProductById)

type alias Review =
    { author : String
    , content : String
    , rating : Int
    }

type alias Product =
    { id : Int
    , name : String
    , description : String
    , imageUrl : String
    , category : String
    , website : String
    , phone : String
    , address : String
    , hours : List (String, String)
    , reviews : List Review
    }

allProducts : List Product
allProducts =
    [ { id = 1
      , name = "Home Loan"
      , description = "A home loan helps you buy your dream house with flexible repayment options and competitive interest rates. Ideal for first-time buyers and those looking to upgrade."
      , imageUrl = "https://images.unsplash.com/photo-1506744038136-46273834b3fb?auto=format&fit=crop&w=400&q=80"
      , category = "Loan"
      , website = "https://www.bankofamerica.com/mortgage/home-mortgage/"
      , phone = "1-800-123-4567"
      , address = "123 Main St, Anytown, USA"
      , hours = [ ("Monday-Friday", "9:00 AM - 6:00 PM"), ("Saturday", "10:00 AM - 2:00 PM"), ("Sunday", "Closed") ]
      , reviews = [ { author = "Alice", content = "Easy process and great rates!", rating = 5 } ]
      }
    , { id = 2
      , name = "Car Loan"
      , description = "Finance your new or used car with our quick approval car loans. Enjoy low interest rates and flexible terms for all types of vehicles."
      , imageUrl = "https://images.unsplash.com/photo-1511918984145-48de785d4c4e?auto=format&fit=crop&w=400&q=80"
      , category = "Loan"
      , website = "https://www.chase.com/personal/auto-loans"
      , phone = "1-800-234-5678"
      , address = "456 Elm St, Anytown, USA"
      , hours = [ ("Monday-Friday", "9:00 AM - 5:00 PM"), ("Saturday", "10:00 AM - 1:00 PM"), ("Sunday", "Closed") ]
      , reviews = [ { author = "Bob", content = "Fast approval and helpful staff.", rating = 4 } ]
      }
    , { id = 3
      , name = "Personal Loan"
      , description = "Get instant funds for your personal needs—medical, travel, or emergencies. No collateral required and quick disbursal."
      , imageUrl = "https://images.unsplash.com/photo-1464983953574-0892a716854b?auto=format&fit=crop&w=400&q=80"
      , category = "Loan"
      , website = "https://www.wellsfargo.com/personal-credit/personal-loan/"
      , phone = "1-800-345-6789"
      , address = "789 Oak St, Anytown, USA"
      , hours = [ ("Monday-Friday", "8:00 AM - 7:00 PM"), ("Saturday", "9:00 AM - 2:00 PM"), ("Sunday", "Closed") ]
      , reviews = [ { author = "Carol", content = "Very convenient and fast!", rating = 5 } ]
      }
    , { id = 4
      , name = "Education Loan"
      , description = "Pursue your academic dreams with our education loans. Cover tuition, living expenses, and more with flexible repayment after graduation."
      , imageUrl = "https://images.unsplash.com/photo-1465101046530-73398c7f28ca?auto=format&fit=crop&w=400&q=80"
      , category = "Loan"
      , website = "https://www.salliemae.com/student-loans/"
      , phone = "1-800-456-7890"
      , address = "321 Maple St, Anytown, USA"
      , hours = [ ("Monday-Friday", "9:00 AM - 6:00 PM"), ("Saturday", "Closed"), ("Sunday", "Closed") ]
      , reviews = [ { author = "Dave", content = "Helped me finish college!", rating = 5 } ]
      }
    , { id = 5
      , name = "Business Loan"
      , description = "Grow your business with our flexible business loans. Suitable for startups, expansions, and working capital needs."
      , imageUrl = "https://images.unsplash.com/photo-1508385082359-f48b1c1b1f57?auto=format&fit=crop&w=400&q=80"
      , category = "Loan"
      , website = "https://www.sba.gov/funding-programs/loans"
      , phone = "1-800-567-8901"
      , address = "654 Pine St, Anytown, USA"
      , hours = [ ("Monday-Friday", "9:00 AM - 5:00 PM"), ("Saturday", "Closed"), ("Sunday", "Closed") ]
      , reviews = [ { author = "Eve", content = "Perfect for my small business expansion.", rating = 4 } ]
      }
    ]

getProductById : Int -> Maybe Product
getProductById id =
    List.filter (\p -> p.id == id) allProducts |> List.head 